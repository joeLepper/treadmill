---
auto_merge: false
---

# Plan: Self-dispatching triage with Playwright validation

- **Status:** drafting
- **Date:** 2026-05-29
- **Related ADRs:** ADR-0061 (role-ui-triage labelable visual-bug detection), ADR-0035 (scheduler), ADR-0057 (synthetic-task path for scheduled dispatches)

## Goal

Make the triage worker queue its own follow-up work without an operator gate. After the v1.2 prompt + permissions allowlist landed (PRs #90, #91), the cybernetic loop runs end-to-end *up to* the labelable finding. We now close the next gap: when a finding's policy says `dispatched`, the worker authors a Plan, submits it, and stamps the finding with the resulting `plan_id` — all in the same run. The Plan template for UI-fix dispatches carries a Playwright validation step so the fix is checked against the running dashboard, not just the unit tests.

## Success criteria

- A wf-ui-triage run that produces a `dispatched`-policy finding also POSTs a `triage_findings` row whose `dispatched_plan_id` is non-null, AND a Plan exists at that ID whose `validation` script drives Playwright against the target URL.
- The new `wf-author` task spawned by that Plan opens a PR that ships the fix; `wf-validate` runs Playwright headless against the live (post-merge-of-its-own-PR) dashboard and either passes or fails on the actual rendered behavior, not a unit test alone.
- The Pydantic `TriageFinding` validator accepts `dispatch_action='dispatched'` with a non-null `dispatched_plan_id` set at creation (loosening the prior "always null on emit" rule).
- `triage_findings.run_id` always matches a known `workflow_runs.id` for runs produced by the worker (run_id contract clamp).
- Manual smoke: trigger `wf-ui-triage` against the dashboard once; within the same task, exactly one of the dispatched findings results in a Plan and a downstream task spawning.

## Constraints / scope

### In scope

- `services/api/treadmill_api/schemas/triage_finding.py` — loosen the `dispatched_plan_id` validator; add `run_id`-validity check at insert time.
- `services/api/treadmill_api/prompts/role_ui_triage_v1.md` + canonical `docs/triage/role-ui-triage.v1.md` — v1.3 prompt adds a Dispatching section: when policy says `dispatched`, author a Plan from the bundled template, `treadmill plan submit` it, capture `plan_id`, write it into the finding before POSTing. Anti-author rules stay; the only new authoring permission is Plan-doc authoring + `treadmill plan submit`.
- `workers/agent/scripts/triage/plan-template-ui-fix.md` — short Plan template with a Playwright validation step. Lives next to the existing probe scripts; bundled into the image at `/opt/triage/`.
- `.claude/settings.json` — add `Bash(treadmill plan submit *)` and `Bash(treadmill plan validate *)` to the permissions allowlist.
- `services/api/AGENT.md`, `workers/agent/AGENT.md`, `docs/triage/AGENT.md` (if it exists; create if not) — Recent-changes entry per touched component.
- `services/api/tests/test_triage_finding_schema.py` — pin the new dispatched+plan_id and run_id-validity rules.
- `services/api/tests/test_role_ui_triage_prompt_matches_canonical_doc.py` — existing lockstep test stays green by definition since we update both copies.

### Out of scope

- Separate `wf-ui-triage-dispatch` workflow. We rejected the detector/dispatcher split in ADR-0061's *"One role, not two"* section (re-examined 2026-05-29). Single role, single workflow, single run.
- Auto-merge of triage-dispatched PRs. `auto_merge: false` stays on these Plans while the sibling RAMJAC session is live (per [[feedback-concurrent-orchestrators]]).
- Dashboard surfacing of dispatched-by-triage findings. The labeling UI already shows them; a separate "spawned by triage" view is a future ADR.
- Self-deduplication beyond the existing `duplicate_open_pr` + `duplicate_recent_finding` signals. If two triage runs both dispatch fixes for the same bug, the second's PR collides with the first's open PR — the existing dedup catches it.

### Budget

One worker task. If it exceeds 3 architect-amend cycles, abort and dispatch the steps individually.

**Schema note** — re-examined the `TriageFinding._check_dispatched_plan_id` validator: it already correctly requires `dispatched_plan_id` to be non-null when `dispatch_action='dispatched'`. The v1.2 worker downgraded to `research_only` because it couldn't produce a plan_id, not because the validator was wrong. We do not loosen the validator. The v1.3 prompt teaches the worker how to author + submit a Plan and capture the resulting `plan_id` before POSTing. The `run_id` contract clamp (follow-up #1 from the v1.2 post-run report) is a separate follow-up; deferred.

## Sequence of work

```yaml
sequence_of_work:
  - id: role-ui-triage-v1.3-self-dispatching
    title: "ADR-0061 v1.3 — self-dispatching triage + ui-fix Plan template with Playwright validation"
    workflow: wf-author
    intent: |
      STUDY (shape references, do not modify):
        - `services/api/treadmill_api/schemas/triage_finding.py`
          lines 128-142 — the `_check_dispatched_plan_id`
          model_validator that v1.3 must satisfy (dispatched ⇒
          plan_id present). Already correct; v1.3 just makes the
          worker capable of satisfying it.
        - `docs/triage/role-ui-triage.v1.md` — current v1.2 prompt.
          The v1.3 changes are additive: keep the "What you exist
          to do" / "What you must NEVER do" / "Network mapping" /
          "Required reading" / dispatch-policy / anti-loop / exit-
          criterion sections intact. Add one new "Dispatching"
          section just before "Run exit criterion". Bump version
          contract paragraph at the end. Bump prompt_version
          example to v1.3.0.
        - `docs/adrs/0061-role-ui-triage-labelable-visual-bug-detection.md`
          (amended 2026-05-29) — the "Decision" §5 paragraph and
          new "UI-fix dispatched plans validate via Playwright"
          sub-section name the contract this task implements.
        - `workers/agent/Dockerfile` — already has
          `COPY workers/agent/scripts/triage/ /opt/triage/`. The
          new plan template file will land in that source dir;
          no Dockerfile edit required unless the COPY's source
          dir changed.
        - `cli/treadmill_cli/cli.py` for the `plan submit` and
          `plan validate` command shapes the v1.3 prompt cites.

      BUILD:

      (1) `workers/agent/scripts/triage/plan-template-ui-fix.md` —
          NEW bundled Plan template. Single-task plan template
          targeting `wf-author` with placeholder slots the triage
          worker fills in per finding. The `validation` block must
          show a Playwright-driven deterministic gate that targets
          `http://treadmill-dashboard:80/` (container-DNS hostname,
          NOT `localhost:5174` — same translation as the v1.2 prompt
          network-mapping section). Template skeleton:

          ```yaml
          sequence_of_work:
            - id: triage-fix-<FINDING_ID_SHORT>
              title: "ui-fix from triage finding <FINDING_ID_SHORT> — <SHORT_OBSERVATION>"
              workflow: wf-author
              intent: |
                STUDY:
                  - <EVIDENCE_POINTER>

                BUILD:
                  <PROPOSED_RESOLUTION>

                TESTS:
                  - The Playwright validation script below must
                    pass against the live dashboard at
                    http://treadmill-dashboard:80/ after the fix
                    lands.

                DOC:
                  - Update the touched component's AGENT.md
                    Recent-changes entry citing this triage
                    finding's <FINDING_ID_SHORT>.
              scope:
                files:
                  - <PROPOSED_RESOLUTION_FILES>
                  - <COMPONENT_AGENT_MD>
                services_affected:
                  - services/dashboard
                out_of_scope:
                  - Unrelated dashboard cleanups
                  - Changes to the triage role prompt itself
              validation:
                - kind: deterministic
                  description: "Playwright asserts <FINDING_ID_SHORT> no longer reproduces against http://treadmill-dashboard:80/."
                  script: |
                    node -e '
                      const { chromium } = require("playwright");
                      (async () => {
                        const browser = await chromium.launch({ headless: true });
                        const page = await browser.newPage();
                        await page.goto("http://treadmill-dashboard:80/<TARGET_PATH>", { waitUntil: "networkidle" });
                        <PLAYWRIGHT_ASSERTION_DERIVED_FROM_PROPOSED_RESOLUTION>
                        await browser.close();
                      })().catch(e => { console.error(e); process.exit(1); });
                    '
                  severity: blocking
                  timeout_seconds: 120
          ```

          The literal placeholder tokens (`<FINDING_ID_SHORT>`,
          `<SHORT_OBSERVATION>`, `<EVIDENCE_POINTER>`,
          `<PROPOSED_RESOLUTION>`, `<PROPOSED_RESOLUTION_FILES>`,
          `<COMPONENT_AGENT_MD>`, `<TARGET_PATH>`,
          `<PLAYWRIGHT_ASSERTION_DERIVED_FROM_PROPOSED_RESOLUTION>`)
          are the seams the triage worker fills in per finding —
          DO NOT replace them with real values; they are sentinels.

      (2) `docs/triage/role-ui-triage.v1.md` — v1.3 changes:
          - Change top "# role-ui-triage (v1.2)" to "(v1.3)".
          - Insert a new "## Dispatching" section between
            "## Anti-loop guards" and "## Run exit criterion":

            For each finding whose `dispatch_action == "dispatched"`,
            you author a Plan and capture its `plan_id` BEFORE
            POSTing the finding. Steps, in order:

              a. Read the bundled template at
                 `/opt/triage/plan-template-ui-fix.md`. Copy its
                 body to `/tmp/triage-<run_id>/dispatched/<finding_seq>.md`.
                 Fill every `<...>` placeholder using fields from
                 the finding you're about to dispatch.
                   - `<FINDING_ID_SHORT>` ← first 8 chars of the
                     finding's `finding_id`.
                   - `<SHORT_OBSERVATION>` ← the `observation` field,
                     truncated to 60 chars.
                   - `<EVIDENCE_POINTER>` ← the `evidence_pointer`
                     field verbatim.
                   - `<PROPOSED_RESOLUTION>` ← the
                     `proposed_resolution` field verbatim. Quote it
                     as a multi-line block.
                   - `<PROPOSED_RESOLUTION_FILES>` ← parse the
                     `proposed_resolution` for explicit file paths
                     (e.g. `TaskDetail.tsx`); emit one path per
                     line under `files:`. Always also include the
                     touched component's AGENT.md.
                   - `<COMPONENT_AGENT_MD>` ← the AGENT.md nearest
                     to the proposed-resolution files (e.g.
                     `services/dashboard/AGENT.md`).
                   - `<TARGET_PATH>` ← the path portion of the
                     finding's `target_url` (e.g. `/tasks/<id>` or
                     just `/`).
                   - `<PLAYWRIGHT_ASSERTION_DERIVED_FROM_PROPOSED_RESOLUTION>` ←
                     a `await page.locator(...).waitFor()` or
                     `expect`-like JS asserting the bug no longer
                     reproduces. If the finding is a `dead_affordance`
                     case, assert the affordance now activates; if
                     a `consistency` case, assert the consistency
                     property holds; etc. Derive from the
                     `proposed_resolution`. One assertion per
                     finding; do not over-specify.

              b. Run `treadmill plan validate
                 /tmp/triage-<run_id>/dispatched/<finding_seq>.md`.
                 If it fails, FIX the placeholders and re-run. Do
                 not submit a plan that fails validate.

              c. Run `treadmill plan submit
                 /tmp/triage-<run_id>/dispatched/<finding_seq>.md
                 --auto-merge=false`. Capture the `plan_id` from
                 stdout (look for a UUID; if the CLI prints a JSON
                 envelope, parse `plan_id`).

              d. Set `dispatched_plan_id = <captured_plan_id>` on
                 the finding record before POSTing it to
                 `/api/v1/triage/findings`.

            Constraints:
              - Plan-doc authoring at `/tmp/triage-<run_id>/dispatched/`
                is the SOLE exception to the "Never modify code,
                Never write plan docs" rules — only files under
                that dir, only the bundled template, only one Plan
                per dispatched finding.
              - `auto_merge=false` is REQUIRED on the submit
                command while the sibling RAMJAC session is live.
              - Cap still applies: at most 3 dispatched findings
                per run.
              - If `treadmill plan submit` fails for any reason,
                downgrade the finding to `research_only` (set
                `dispatched_plan_id = null` and update
                `dispatch_reason` to note the submit failure) and
                continue. Do NOT block the run.

          - Update version-contract paragraph at the end:
            "v1.3.0 adds inline self-dispatching: the worker
            authors a Plan from the bundled UI-fix template and
            captures the resulting `plan_id` before POSTing
            dispatched findings. The downstream Plan's validation
            step drives Playwright against the live dashboard."
          - Bump `prompt_version` example to `v1.3.0`.

      (3) `services/api/treadmill_api/prompts/role_ui_triage_v1.md` —
          mirror of (2). Byte-equality enforced by the existing
          `test_role_ui_triage_prompt_matches_canonical_doc` test.

      (4) `.claude/settings.json` `permissions.allow` — add:
          - `Bash(treadmill plan submit *)`
          - `Bash(treadmill plan validate *)`
          Keep all existing entries.

      (5) AGENT.md updates:
          - `services/api/AGENT.md` Recent changes — bump the
            ADR-0061 v1.2 entry to add a v1.3 amendment paragraph
            describing the Dispatching section, the inline Plan
            authoring + submit path, the `dispatched_plan_id`
            capture, and the now-satisfiable
            `_check_dispatched_plan_id` validator.
          - `workers/agent/AGENT.md` Recent changes — add an entry
            for the new bundled Plan template
            (`/opt/triage/plan-template-ui-fix.md`) and its
            Playwright validation skeleton.

      TESTS:
        - Existing `test_role_ui_triage_prompt_matches_canonical_doc`
          stays green by definition since both files move together.
        - Existing `test_role_ui_triage_is_seeded` checks
          startswith("# role-ui-triage"); the v1.3 prompt still
          satisfies this.
        - NEW deterministic check in the validation block of THIS
          plan: grep for the new Dispatching section + v1.3.0
          version markers in both prompt copies.
        - NO unit test for the Plan template content — it's a
          template the triage worker interpolates. A schema check
          on its presence (`ls workers/agent/scripts/triage/plan-template-ui-fix.md`)
          is sufficient.

      DOC SCOPE NOTE:
        - `docs/adrs/0061-...` is amended IN A SEPARATE COMMIT before
          this task lands (the amendment is operator-authored in
          this conversation, not worker-authored). Do not touch
          the ADR file.
        - `docs/plans/2026-05-29-self-dispatching-triage.md` is THIS
          plan; do not touch.

      OUT-OF-SCOPE (DO NOT TOUCH):
        - The `_check_dispatched_plan_id` model_validator (already
          correct).
        - The `run_id` schema field (separate follow-up).
        - The Pydantic `TriageFinding` model in any other way.
        - The Postgres `triage_findings` table or its migration.
        - Any other role prompt.
        - The labeling UI / `/triage` page.
        - Any dashboard code (those bugs are fixed by the
          downstream Plans this task enables, not by this task
          itself).
    scope:
      files:
        - docs/triage/role-ui-triage.v1.md
        - services/api/treadmill_api/prompts/role_ui_triage_v1.md
        - workers/agent/scripts/triage/plan-template-ui-fix.md
        - .claude/settings.json
        - services/api/AGENT.md
        - workers/agent/AGENT.md
      services_affected:
        - services/api
        - workers/agent
      out_of_scope:
        - services/api/treadmill_api/schemas/triage_finding.py
        - services/api/treadmill_api/models/triage_finding.py
        - cli/treadmill_cli/*
        - services/dashboard/*
        - docs/adrs/0061-role-ui-triage-labelable-visual-bug-detection.md
        - docs/plans/2026-05-29-self-dispatching-triage.md
    validation:
      - kind: deterministic
        description: "Lockstep byte-equality between canonical + bundled prompt copies; both contain the v1.3 Dispatching section + v1.3.0 marker; Plan template exists with placeholder sentinels + container-DNS host; permission patterns present."
        script: |
          set -euo pipefail
          diff -q docs/triage/role-ui-triage.v1.md services/api/treadmill_api/prompts/role_ui_triage_v1.md
          grep -q "^# role-ui-triage" services/api/treadmill_api/prompts/role_ui_triage_v1.md
          grep -q "^## Dispatching" docs/triage/role-ui-triage.v1.md
          grep -q "v1.3.0" docs/triage/role-ui-triage.v1.md
          test -f workers/agent/scripts/triage/plan-template-ui-fix.md
          grep -q "<FINDING_ID_SHORT>" workers/agent/scripts/triage/plan-template-ui-fix.md
          grep -q "treadmill-dashboard:80" workers/agent/scripts/triage/plan-template-ui-fix.md
          grep -q "treadmill plan submit" .claude/settings.json
          grep -q "treadmill plan validate" .claude/settings.json
        severity: blocking
        timeout_seconds: 60
      - kind: llm-judge
        description: "AGENT.md updates land per ADR-0030 in services/api/AGENT.md and workers/agent/AGENT.md."
        prompt: |
          The DIFF should include Recent-changes entries in both
          services/api/AGENT.md and workers/agent/AGENT.md describing
          the role-ui-triage v1.3 / self-dispatching change. Verdict
          'pass' when both AGENT.md files have a new or extended
          Recent-changes entry citing v1.3 or "self-dispatching";
          'fail' otherwise.
        severity: blocking
```

## Diagram

```mermaid
sequenceDiagram
    participant Scheduler
    participant TriageWorker as role-ui-triage v1.3
    participant API as treadmill-api
    participant CLI as treadmill plan submit
    participant DownstreamWorker as role-code-author
    participant Playwright as wf-validate (Playwright)

    Scheduler->>API: tick wf-ui-triage
    API->>TriageWorker: run start
    TriageWorker->>TriageWorker: probe.mjs + evidence_summary.json
    TriageWorker->>TriageWorker: classify → dispatch_action="dispatched"
    TriageWorker->>CLI: treadmill plan submit (ui-fix template)
    CLI->>API: POST /api/v1/plans
    API-->>CLI: plan_id
    TriageWorker->>API: POST /api/v1/triage/findings {dispatched_plan_id: plan_id}
    API-->>TriageWorker: 201
    Note over API: Plan auto-creates task; SQS dispatch
    API->>DownstreamWorker: wf-author step
    DownstreamWorker->>DownstreamWorker: edit + commit + open PR
    DownstreamWorker->>Playwright: wf-validate
    Playwright->>Playwright: assert bug no longer reproduces
    Playwright-->>API: validation verdict
```

## Risks / unknowns

- **Prompt comprehension at v1.3 length.** v1.2 already restructured the prompt around comprehension order; adding a Dispatching section grows it ~30%. Mitigation: keep the new section near the bottom, mark it as the *only* exception to the anti-author rules. Abort trigger: if the v1.3 run regresses to v1.0/v1.1 behavior (writes code or skips POST), revert to v1.2 + author the async-dispatcher workflow instead.
- **`treadmill` CLI in the worker image.** The agent image installs the CLI for plan-validate (per `project_treadmill_plan_validate_command_todo`); confirm `treadmill plan submit` works inside the worker against the API at `http://treadmill-api:8088`. Mitigation: if not, add a curl-based POST to /api/v1/plans as the Dispatching path instead.
- **Playwright in the downstream `wf-validate` step.** The agent image already ships Playwright + chromium-headless-shell (ADR-0061 Step 2). The Plan template's validation script just needs to invoke it; no new image work. Risk: dashboard isn't reachable from the worker container at validation time. Mitigation: same `http://treadmill-dashboard:80/` mapping the triage prompt's network-mapping section already documents.
- **Loops.** A finding that triggers a dispatch that fails to fix the bug, on the next triage tick, would dispatch again. Mitigation: existing `duplicate_open_pr` + `duplicate_recent_finding` signals already suppress this; verify by checking the next scheduled tick after a smoke-test dispatch.

## Decisions captured during execution

_To be appended as we work._

## Post-mortem

_To be filled when the plan transitions to `completed` or `abandoned`._
