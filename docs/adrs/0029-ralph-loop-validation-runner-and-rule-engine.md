# ADR-0029: Ralph-loop validation runner + rule engine

- **Status:** Proposed (drafted 2026-05-13)
- **Date:** 2026-05-13
- **Related:** ADR-0006 (rule primitive — §"Engine deferred" is what this ADR answers), ADR-0007 (Bunkhouse-precedent validation runner pattern), ADR-0013 (mergeability VIEW reads `validate.decision`), ADR-0015 (wf-validate placeholder; `validation:` block contract), ADR-0022 (deliberate omission of a `validation` output kind), ADR-0026 (dispatch dedup), ADR-0027 (JSON envelope precedent for LLM verdicts), task #101.

## Context

Treadmill's `wf-validate` workflow has been a placeholder since the
Phase-2 closure (per ADR-0015 §"role-validator"). The role's prompt
explicitly says *"Do NOT fabricate a pass/fail verdict — say
'placeholder' explicitly."* The mergeability VIEW (ADR-0013) reads
`validate.decision`, so until a real verdict lands, every PR sits at
`pending` against the validate dimension and the merge gate never
flips clean from the validation side.

ADR-0006 defined a **rule primitive** — a YAML document at
`docs/knowledge-base/rules/<slug>.yaml` with `deterministic` and
`llm-judge` checks — but explicitly deferred the engine: *"This ADR
defines the primitive, not the engine that evaluates rules."* The
engine has stayed deferred through Phase 3 + 4.

The May 13 2026 smoke session put concrete pressure on both gaps.
Across five PRs produced this session (#18, #20, #21, #23, plus the
two re-fires), three exhibited the same bug class:

1. **PR #18** (Wire OTel SDK): `wf-author` requested
   `opentelemetry-instrumentation-boto3` — a package that doesn't
   exist on PyPI. `wf-review` approved it. `uv lock` failed at
   `treadmill-local up` time.
2. **PR #20** (Provision API IAM user): the test `wf-author` wrote
   assumed CDK Resource ARNs would be literal strings. CDK
   synthesizes them as `Fn::Sub` / `Fn::Join` / `Fn::GetAtt` dicts.
   `pytest` crashed with `TypeError: unhashable type: 'dict'`.
3. **PR #23** (Provision deploy-events queue): used
   `aws_cdk.aws_sns_subscriptions.SubscriptionFilter` — the
   `SubscriptionFilter` class lives in `aws_cdk.aws_sns`, not in
   `aws_cdk.aws_sns_subscriptions`. `cdk synth` failed.

In each case `wf-review` approved cleanly via the ADR-0027 JSON
envelope because the *content* matched the spec. The bug is **the
loop has no mechanical gate that actually runs the code**. A
deterministic `pytest` / `uv lock` / `cdk synth` invocation would
have caught all three.

The handoff at
`docs/handoffs/2026-05-13-ralph-loop-scoping-signal.md` catalogues
five structural holes. ADR-0029 addresses the validation-runner
half of the closure; auto-merge (hole adjacent to this set) is its
own ADR; the trigger-evaluator deferred-run dispatch fix (hole 4)
is a bounded sibling fix.

## Goal

A validation gate that **executes** checks against a PR — not
evaluates them semantically — and feeds the result into the
mergeability VIEW. Specifically:

* When a PR opens or synchronizes, `wf-validate` fires (already true
  per the trigger evaluator's `pr_synchronize` fan-out).
* The validate runner pulls the task's `task_validations` rows plus
  any rules whose `applies_to:` matches the PR, and executes each.
* Deterministic checks run as subprocesses; LLM-judge checks run
  Claude Code with the check's prompt + PR diff as input.
* Per-task aggregate verdict (worst wins: `error` > `fail` > `pass`)
  becomes the wf-validate `step.completed` envelope's `decision`.
* On `fail` or `error`, `wf-feedback` dispatches to address.
* On `pass`, the mergeability VIEW can flip to `mergeable` (subject
  to wf-review + CI + no conflict).

## Decision

### One worker disposition, two execution modes

The validation runner is the **first non-Claude-driven disposition
handler** in the worker. Existing dispositions (`code`, `review`,
`analysis`, `plan_doc`) all begin with a Claude Code session and
then post-process its output. `wf-validate` is different: each
check is either a subprocess invocation (`kind=deterministic`) or
a Claude Code session targeted at *that specific check's prompt*
(`kind=llm-judge`). The shared prefix doesn't apply.

Implementation shape:

* `workers/agent/treadmill_agent/runner_dispositions/validation.py`
  is added with a `handle()` entry point. Routing into it is
  keyed on `workflow_id == 'wf-validate'`, not on `output_kind`
  (per ADR-0022's deliberate omission of a `validation` output
  kind — validation is an architectural pattern, not a
  Claude-output shape).
* `handle()` iterates the validation set, runs each per its
  `kind`, captures the per-check verdict + log output, aggregates
  worst-wins, returns a `StepOutput` with `decision ∈ {pass, fail,
  error}` per ADR-0015's value-set.

### Project-agnosticism is load-bearing

Treadmill is an agentic runner for **arbitrary** projects. It ships
no hardcoded checks. Per-language tooling (`pytest`, `uv`, `cdk`,
`go test`, `cargo test`, `npm test`, …) lives in **operator-authored
rules in the project's repo**, not in Treadmill.

Concretely: the validation runner is a primitive — *run this
command, capture exit code* (deterministic) or *spawn Claude with
this prompt, capture verdict* (llm-judge). The set of checks is
populated by:

1. **Per-task `validation:` blocks** in the plan doc (operator
   declares inline; persisted to `task_validations` rows by the
   plan-doc parser).
2. **Rules at `docs/knowledge-base/rules/<slug>.yaml`** with
   `applies_to:` selectors (operator authors per-repo; the rule
   engine matches them to applicable tasks at validation time).

The runner doesn't care whether a check came from a plan-doc
block or a rule. Both surfaces produce the same shape:
`(kind, description, script | prompt)`.

This **closes ADR-0006 §"Engine deferred"**: the engine is the
validation runner's per-task rule-matching pass.

### Schema changes — `task_validations` carries the check content

Today `task_validations` has `kind` + `description` but no
content carrier. The parser comment notes *"`script` is added in
Phase 4 alongside the rule engine"* — that's now.

Alembic migration 0011 adds:

* `script` `text NULL` — set when `kind='deterministic'`. The
  command line to execute, relative to the cloned repo root.
  Example: `uv run pytest services/api/tests/test_starters.py`.
* `prompt` `text NULL` — set when `kind='llm-judge'`. The
  natural-language criterion the LLM judge evaluates.

CHECK constraint extension: `(kind='deterministic' AND script IS
NOT NULL) OR (kind='llm-judge' AND prompt IS NOT NULL)` —
malformed rows can't enter.

The plan-doc parser's `TaskValidationCheck` Pydantic model gains
`script: str | None` and `prompt: str | None` with the same
constraint.

### Single fire per SHA — no retry

Per operator framing 2026-05-13: `wf-validate` runs exactly once
against any given SHA. A re-run against the same SHA would
produce the same result; retrying is wasteful.

Convergence is driven by `wf-feedback`, not by wf-validate retry:

* `wf-validate` returns `fail` → trigger evaluator dispatches
  `wf-feedback` (need to add `wf-validate.step.completed` with
  `decision='fail'` as a trigger source, analogous to the
  `pr_review_submitted → wf-feedback` and #108 path 1's
  decision-based self-trigger for `wf-review` changes_requested).
* `wf-feedback` analyzer reads the failing check's logs + posts a
  task directive; action role re-authors.
* New commit on the PR's branch → `pr_synchronize` → `wf-validate`
  fires fresh against the new SHA.
* Loop continues until either (a) `wf-validate` returns `pass`,
  or (b) `wf-feedback`'s cap fires (TBD — Open Q29.e).

`wf-validate` itself has no cap (per ADR-0013 it must re-fire on
every `pr_synchronize` for HEAD invalidation correctness). The
*budget* lives in `wf-feedback`'s cap, not in wf-validate's.

### Verdict aggregation: worst wins

A task can have N task_validations + M applicable rules. The
runner executes all N+M checks and aggregates:

| Per-check verdict | Aggregate verdict (worst so far) |
| --- | --- |
| any `error` | `error` |
| (no `error`) any `fail` | `fail` |
| (no `error`, no `fail`) all `pass` | `pass` |

`error` means the runner itself failed (subprocess crashed before
producing a verdict; LLM-judge returned unparseable output).
`fail` means the check ran cleanly and returned non-zero / verdict
of `fail`. Distinguishing the two lets the operator separate
"my check is busted" from "the code is busted."

### What this ADR does NOT do

* **Auto-merge.** When `task_mergeability.derived_mergeability =
  'mergeable'`, today the operator merges. ADR-0031 (TBD) covers
  the auto-merger. ADR-0029 stops at "validation produced its
  verdict + mergeability VIEW reflects it."
* **GitHub check_run posting.** Useful for human observers on the
  PR page; not load-bearing for mergeability (which reads
  Treadmill's own envelope). Deferred — Open Q29.d.
* **Bunkhouse-style S3-cached scripts + cross-account roles.** Per
  ADR-0007. Right answer for `fully_remote`; overkill for v0
  dev-local. The validation worker reads `script` text from
  `task_validations` and executes inside its own container against
  the cloned repo.
* **Fixing hole 4** (reevaluate doesn't pick up deferred runs).
  Sibling fix; bounded; not part of ADR-0029.
* **wf-author empty-diff softening** (hole 2). Operator deferred
  2026-05-13; not in scope.

## Worker-side shape

`workers/agent/treadmill_agent/runner_dispositions/validation.py`:

```python
def handle(ctx: DispositionContext) -> StepOutput:
    """Iterate task_validations + applicable rules; execute each;
    aggregate worst-wins; return StepOutput with decision."""
    checks = _gather_checks(ctx)  # task_validations + matching rules
    verdicts: list[CheckResult] = []
    for check in checks:
        if check.kind == "deterministic":
            verdicts.append(_run_deterministic(check, ctx.repo_dir))
        elif check.kind == "llm-judge":
            verdicts.append(_run_llm_judge(check, ctx))
    decision = _aggregate(verdicts)  # error > fail > pass
    return StepOutput(
        summary=_compose_human_summary(verdicts),
        decision=decision,
        commit_sha=ctx.ctx.head_sha,  # ADR-0014 — top-level for VIEW
        artifacts=[],
        payload={"checks": [v.model_dump() for v in verdicts]},
        metadata=Metadata(extra={"checks_run": len(verdicts)}),
    )
```

Per-kind execution:

* **Deterministic** — subprocess.run with the check's `script`
  string; cwd=cloned repo; capture stderr+stdout; exit code 0
  = `pass`, non-zero = `fail`; subprocess error (FileNotFound,
  permission, timeout) = `error`. 5-minute per-check timeout.
* **LLM-judge** — spawn Claude Code (same boundary as the existing
  Claude dispositions; reuse `claude_code.run_claude`) with a
  composed prompt: `{check.prompt}\n\nPR diff:\n{diff}\n\nTask
  spec:\n{spec}`. Output must end with a JSON envelope —
  `ValidationVerdict` (sibling to ADR-0027's `ReviewVerdict`):

  ```python
  class ValidationVerdict(BaseModel):
      verdict: Literal["pass", "fail"]
      rationale: str = Field(max_length=4000)
  ```

  Parse-failure → `error`; `verdict=fail` → `fail`; `verdict=pass`
  → `pass`. The body-stripping pattern from ADR-0027 applies.

## Convergence trigger: `wf-validate.fail → wf-feedback`

Adds a third dispatch path for `wf-feedback` alongside the existing
two:

| Trigger source | Dedup key namespace |
| --- | --- |
| `pr_review_submitted` webhook (external human reviewer) | `review=<github_review_id>` |
| `wf-review.step.completed` with `decision='changes_requested'` (#108 path 1) | `review-run=<wf_review_run_id>` |
| **`wf-validate.step.completed` with `decision='fail'` (new)** | `validate-run=<wf_validate_run_id>` |

Implementation: extend
`coordination/triggers.maybe_dispatch_feedback_on_review_changes_requested`
into a generalized
`maybe_dispatch_feedback_on_terminal_failure(step_id, typed,
workflow_id, fail_decision)` that handles all three. Update
`dispatch_dedup._build_wf_feedback_key` for the third namespace.

The wf-feedback analyzer's prompt extends to handle the new input
shape: it now reads either a review comment (existing) OR a
validation failure log. The action role-code-author's prompt is
unchanged — it already takes a task directive.

## Trade-offs

* **The worker container needs to be able to run arbitrary
  scripts.** Per-language tooling (Python+uv, Node+npm, …) must be
  installed in the worker image. For Treadmill self-hosting, our
  worker already has uv + pytest + cdk; that's enough for our own
  rules. For managing other projects (#95), the worker image
  pattern needs expanding.
* **LLM-judge cost is per-task.** A task with 3 llm-judge rules
  pays 3 Claude calls beyond the wf-review + wf-author cost. At
  haiku-tier this is manageable; opus-tier judges would not be.
  The check schema should let the rule author pick the model.
  Open Q29.b.
* **wf-validate single-fire-per-SHA simplifies the loop.** No retry
  budget to track for wf-validate itself. wf-feedback drives
  convergence; wf-feedback's existing cap (TBD per Open Q29.e) is
  the loop termination.
* **Rule engine is fired at wf-validate time, not at PR-open.** An
  alternative was rule-pre-evaluation at task creation (write
  matched rules' checks as `task_validations` rows when the plan
  is parsed). Rejected: PR diff content is unknown at plan-parse
  time, and `applies_to:` patterns may want to match diff-content
  (e.g., "tests that touch the consumer's webhook path"). Firing
  the rule engine at validation time gives it access to the actual
  diff.
* **Project-agnosticism puts the configuration cost on the
  operator.** Treadmill ships no out-of-the-box "Python project
  starter" rules; the operator authors rules per-repo. For the
  Treadmill repo itself, that means writing rules for our own
  validations as part of ADR-0029's landing. Acceptable cost;
  rules are small YAML files.

## Alternatives considered

* **Hardcoded built-ins in Treadmill** (`pytest --collect-only` on
  any `*.py` change; `uv lock --check` on `pyproject.toml`;
  `cdk synth` on infra). Cheapest path; would have caught the
  three hallucinated-API bugs this session. Rejected per the
  agnosticism principle — bakes Python/uv/CDK assumptions into a
  runner that's meant for arbitrary projects. The same rules can
  be authored as project-local YAMLs.
* **A `validation` OutputKind on roles.** ADR-0022 deliberately
  rejected this; rehashing it here would re-introduce the
  conflation between "what does Claude's output look like" and
  "what does the worker do with the role." Routing by
  `workflow_id` instead is the correct seam.
* **Run checks in a sidecar / S3 / cross-account** per ADR-0007's
  bunkhouse precedent. Right shape for `fully_remote`; overkill
  for dev-local. The worker container is the runner.
* **Pre-evaluate rules at task creation, persist matched checks
  to task_validations.** See trade-offs above — rejected because
  diff content isn't available at parse time.
* **Skip llm-judge at v0; only deterministic.** Tempting (cheaper,
  more reproducible). Rejected because many of the checks we want
  are inherently semantic ("does this test exercise the change's
  risk surface?"). The Ralph-loop framing in ADR-0001 commits to
  hybrid.

## Open Qs

* **Q29.a — Rule storage location.** Today's ADR-0006 says
  `docs/knowledge-base/rules/<slug>.yaml`. For the Treadmill repo
  managing itself that's fine; rules live in the repo being
  validated. For managing OTHER repos (#95, bootstrap
  non-Treadmilled repos), where do rules come from — the managed
  repo, a per-deployment config in Treadmill's DB, or both? Lean:
  both; rules in the managed repo apply locally, rules in the
  deployment config apply across all managed repos for that
  deployment.

* **Q29.b — LLM-judge model selection.** Should `check.llm_model`
  be a per-rule field (cheap haiku vs expensive opus), or should
  Treadmill pick a default and let rules opt out? Lean: rules
  specify; default is the deployment's `WORKER_MODEL` (haiku).

* **Q29.c — Per-check timeout.** Subprocess timeout for
  deterministic checks. 5 min is a guess. Lean: per-rule
  `timeout_seconds:` field with a global default.

* **Q29.d — GitHub check_run posting.** Tells human observers
  what failed without going through the API. Not load-bearing
  for mergeability. Lean: deferred; add when needed; the runner
  posts a single `gh pr comment` with the check summary (same
  shape as ADR-0027's review comment) until then.

* **Q29.e — wf-feedback cap.** Per ADR-0015, `wf-feedback` is
  uncapped today (only `wf-ci-fix` + `wf-conflict` are capped at
  3). With the new `wf-validate.fail → wf-feedback` path, the
  loop can in principle run unbounded. Lean: cap wf-feedback at
  5 attempts per task. After cap, surface to operator as
  `task.capped`.

* **Q29.f — Aggregation of `pass` checks with `advisory` /
  `warning` severity.** ADR-0006's rule schema has `severity:
  blocking | warning | advisory`. Today's `verdict_aggregate`
  treats all `fail`s equally. Lean: only `severity=blocking`
  fail/error block merge; warning + advisory surface in the
  envelope payload but don't gate.

* **Q29.g — Sharing the deterministic + llm-judge code with
  bunkhouse / future fully_remote.** ADR-0007's bunkhouse runner
  has S3-cached scripts + cross-account roles. The v0 dev-local
  runner reads `script` text directly + executes in-process.
  When `fully_remote` lands, the same handler interface can swap
  underlying execution (S3 fetch vs in-DB script) without
  changing the worker boundary.

## Phasing

1. **Schema** — alembic 0011: add `script` + `prompt` columns +
   the kind/content CHECK extension.
2. **Parser extension** — `TaskValidationCheck` gains `script` +
   `prompt`; persistence in `routers/plans.py` writes them.
3. **Worker disposition** — `validation.py` with deterministic +
   llm-judge subroutines; routing in `runner.py` keyed on
   `workflow_id`.
4. **role-validator reclassification** — drop the placeholder
   prompt; the role isn't really running a Claude session for
   the wrapper case (only for llm-judge checks). Per Q29's
   resolution: keep the role for ADR-0015 schema compatibility
   but mark it as a structural artifact.
5. **Convergence trigger** —
   `maybe_dispatch_feedback_on_terminal_failure` handles the
   third dispatch path; dedup builder extended for
   `validate-run=` namespace.
6. **Treadmill self-hosting rules** — author
   `docs/knowledge-base/rules/python-tests-pass.yaml` and
   siblings so the Treadmill repo has the validations it would
   have wanted to catch this session's bugs. Rules-as-data
   matures with usage.
7. **Smoke** — fire a known-broken PR (e.g., reintroduce one of
   the hallucinated-API bugs deliberately) and verify the loop
   converges via wf-validate.fail → wf-feedback → re-author.
