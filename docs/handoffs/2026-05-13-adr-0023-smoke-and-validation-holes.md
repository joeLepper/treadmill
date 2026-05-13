# Session handoff — 2026-05-13 (ADR-0023 activation smoke + the validation holes it exposed)

## What ran

Activated ADR-0023 (API IAM-user credentials) via the plan-merge
trigger. Watched Treadmill build Treadmill end-to-end. The
infrastructure layer worked perfectly; the model-quality /
convergence layer surfaced three new holes that frame what ADR-0029
(Ralph-loop validation, #101) needs to address.

## Sequence observed

```
PR #19 merged (plan flip)
  └─ plan-merge trigger → 4 tasks created from ADR-0023 plan
     ├─ task 1: Provision API IAM user + secret via CDK
     │   ├─ wf-author → opened PR #20 ✓
     │   ├─ wf-review → request_changes (real nit; reviewer caught a
     │   │   wrong comment about "single statement" in the policy) ✓
     │   ├─ wf-feedback self-trigger → fired via decision='changes_requested'
     │   │   on the wf-review step-output (#108 path 1's new wrinkle) ✓
     │   │   dedup keyed: wf-feedback:joeLepper/treadmill:review-run=<id>
     │   ├─ wf-feedback analyzer → decision=plan-ready ✓
     │   └─ wf-feedback action → FAILED ("Claude Code produced no
     │       changes to commit") ✗
     ├─ task 2/3/4: all wf-author runs FAILED ("no changes to commit")
         (depends_on annotation in plan not enforced at dispatch;
         they ran against stale main without task 1's PR merged)
```

The operator manually fixed the comment-nit + a CDK-token assertion
bug in the test wf-author had generated, merged PR #20.

## Infrastructure that worked end-to-end

- Plan-merge trigger (ADR-0021): `pr_merged` on plan-doc → tasks
  materialized + workflow runs dispatched.
- wf-author on substantive work (CDK + IAM policy, ~140 LOC of
  Python + ~180 LOC of tests).
- wf-review using `gh pr comment` + JSON envelope (#108 + ADR-0027):
  verdict parsed, rationale flowed through StepOutput, comment body
  posted with header + stripped JSON fence.
- **wf-feedback self-trigger** (#108 path 1, validation goal of
  task #115 — now `completed`): the decision-based step-output
  trigger fired correctly off `wf-review.step.completed` with
  `decision='changes_requested'`. No `pr_review_submitted` webhook
  needed.
- ADR-0026 dedup with the new `review-run=<wf_review_run_id>`
  namespace: dispatched exactly once.
- Auto-seed (ADR-0028 Q28.a (ii)): on a fresh DB, the API loaded
  the new JSON-envelope-emitting `role-reviewer` prompt from
  `starters.py` without operator intervention.

## Three new structural holes the smoke exposed

These all rhyme into the next ADR's scope.

### Hole 1: `depends_on` is annotation-only at runtime (#116)

The plan-doc parser reads `depends_on: task.<id>.pr_merged` but the
dispatch path doesn't gate on it. All four tasks' wf-author runs
materialized at plan-trigger time; the autoscaler max=1 serialized
them but ran them against unchanged main.

Result: tasks 2-4 all failed with "no changes to commit" because
the upstream CDK changes weren't in main yet.

The right fix probably: gate `step.ready` emission on dependency
completion, or have the runner check `depends_on` at start and
publish a clean `step.skipped` if unmet.

### Hole 2: wf-feedback can't recover from an empty-diff action

The analyzer correctly identified the comment-nit and produced a
`task_directive`. The action role (role-code-author) couldn't (or
chose not to) make the change — the disposition handler raises
`CodeAuthorError` on empty diff, marking the wf-feedback run
failed.

ADR-0015 caps `wf-ci-fix` + `wf-conflict` at 3 attempts; nothing
caps `wf-feedback`. But this isn't a retry-budget question —
re-running the action with the same prompt would produce the same
empty diff. The right fix needs validation: did the action's
output address the reviewer's verdict? If yes (even if the diff
is empty because the nit was hallucinated), close cleanly with a
"no-op acceptable" decision. If no, escalate.

This is exactly what the Ralph-loop validation gate is for.

### Hole 3: wf-author generates plausible-looking tests that
fail under CDK token semantics

The test wf-author produced for `test_api_iam_policy_has_required_statements`
assumed `policy.Resource` values were literal ARN strings. CDK
synthesizes cross-resource ARN refs as `Fn::Sub` / `Fn::Join` /
`Fn::GetAtt` dicts. The test crashed with `TypeError: unhashable
type: 'dict'`.

wf-review didn't catch it because the review prompt evaluates the
diff against the task spec + ADRs, not by actually running
`pytest`. A **deterministic** validation check — `pytest infra/tests/`
in a wf-validate runner — would have failed at test-time and
either kicked back through wf-feedback or surfaced to the operator
before merge.

This is the **canonical hybrid validation case** that ADR-0029
(wf-validate runner) needs to address:

- Deterministic checks (script execution, e.g. `pytest`, `mypy`,
  `uv lock`) catch what scripts can catch — the unhashable-type
  TypeError above, the boto3→botocore hotfix from this session,
  hallucinated dependency names.
- LLM-judge checks catch what scripts can't — "does the test
  actually exercise the change's risk surface" vs "does the test
  pass."

The PR #18 botocore hotfix earlier this session was the same
class of bug, caught only because `uv lock` failed at `up` time.
That's accidental safety, not a structural one.

## What ADR-0029 / #101 needs to scope

The smoke gave concrete shape to the validation work:

1. **Worker disposition for `kind='deterministic'`.** The runner
   takes the task_validations row's `script` field (per the
   plan-doc schema), `subprocess.run`s it inside the cloned repo,
   maps exit-code to decision (`pass` / `fail` / `error`). Does
   NOT spawn Claude Code.
2. **Worker disposition for `kind='llm-judge'`.** Spawns Claude
   Code with the check's `prompt` as the task spec; reads the
   JSON envelope verdict (same shape as ADR-0027); maps to
   decision.
3. **check_run posting.** The verdict surfaces as a GitHub
   check_run on the PR's HEAD SHA. Operator-visible without
   reading the API.
4. **Reclassify `role-validator`'s output_kind.** Today's
   placeholder `analysis` becomes a new kind (e.g. `validation`)
   or splits into per-kind paths.
5. **Retry / cap policy.** Same shape as `wf-ci-fix` /
   `wf-conflict`.
6. **The relationship to wf-feedback.** If wf-validate says fail,
   does wf-feedback fire? Per ADR-0015 the loop is
   wf-feedback ↔ wf-author until wf-validate says pass. Need to
   confirm that's still the right shape post-#108.
7. **The relationship to plan-doc validation entries vs
   docs/knowledge-base/rules.** Task-attached validations come
   from plan-doc `validation:` blocks (already persisted). Rule-
   derived validations come from a rule engine (#96, deferred in
   ADR-0006). Two trigger sources, one runner.

## What ADR-0030 / #96 inherits

The rule engine work (ADR-0006 §"Engine deferred") becomes "what
generates additional `task_validations` rows beyond what the plan
doc declares." Likely flows:

- At PR-open time, the rule engine reads
  `docs/knowledge-base/rules/*.yaml`, evaluates which rules apply
  to this PR (based on `applies_to:` globs + diff content), and
  synthesizes `task_validations` rows for each matching rule's
  checks.
- wf-validate's runner picks them up alongside the plan-doc
  validations.

This means **ADR-0029 (wf-validate runner) lands first**; ADR-0030
(rule engine) layers on top.

## Operator action items at session close

- ✅ PR #20 merged (CDK + IAM policy landed).
- ❌ PR for task 2 (treadmill-local init reads new CFN output) —
  doesn't exist; wf-author failed against stale main. Needs
  manual re-dispatch OR depends_on enforcement first.
- ❌ PR for task 3 (local-adapter `_fetch_api_credentials`) —
  same.
- ❌ PR for task 4 (operator runbook) — same.
- ⏳ Manual step before tasks 3-4 land: `aws iam create-access-key`
  + `put-secret-value` against the new API IAM user that CDK
  provisioned in PR #20. This is the operator one-time setup.
- ⏳ ADR-0024 (auto-redeploy on merge) — still `proposed`,
  still drafted-not-implemented. Probably the next ADR to land
  after ADR-0023's plan completes, since "Treadmill builds
  Treadmill" needs both pieces (long-lived API creds + auto
  picking up landed PRs).

## Commits this run

```
b71c023  Hotfix: opentelemetry-instrumentation-boto3 → -botocore
         (PR #18 wf-author hallucinated the package; ADR-0029
         deterministic check would catch via `uv lock`)
5f06478  Activate ADR-0023 + flip plan to active (PR #19)
8285b93  wf-author for task 1 — Provision API IAM user + secret
         via CDK (committed inside PR #20)
dd2f9ca  Manual fix-up: comment-nit + CDK-token test assertion
         (committed inside PR #20 after wf-feedback failed)
<final>  PR #20 squash-merged
```

## Pending tasks list, current state

- #95 Bootstrap non-Treadmilled repos
- #96 Learnings-to-validations pipeline → ADR-0030 (after ADR-0029)
- #98 Observability stack (1/5 tasks merged; 4 pending)
- #101 Ralph-loop validation architecture → ADR-0029 (next)
- #103 Structured step-output parsing ADR
- #104 API credentials long-lived IAM-User keys — ✓ ADR accepted,
  plan active, CDK piece landed (PR #20); 3 sub-tasks still
  pending operator re-dispatch
- #107 docs/runbooks/ reorganization
- #109 Treadmill as a real GitHub App (future cleanup)
- #114 Delete VERDICT regex tourniquet (11 clean JSON-envelope
  runs needed; today's smoke was run 2 of 10 ← +1)
- #116 depends_on isn't runtime-enforced

## Resume next session

```bash
# 1. Bring the stack up.
cd /home/joe/treadmill/tools/local-adapter
uv run treadmill-local up --deployment personal

# 2. Manually populate api-aws-credentials secret (one-time per
#    deployment, post-PR #20 CDK changes):
aws iam create-access-key --user-name treadmill-personal-api \
  --profile treadmill-personal --region us-west-2
# Convert output to {aws_access_key_id, aws_secret_access_key} JSON,
# then:
aws secretsmanager put-secret-value \
  --secret-id treadmill-personal/api-aws-credentials \
  --secret-string '{"aws_access_key_id": "...", "aws_secret_access_key": "..."}' \
  --profile treadmill-personal --region us-west-2

# 3. Re-dispatch tasks 2-4 of the ADR-0023 plan. Either:
#    (a) merge a no-op edit to the plan doc to re-fire the
#        plan-merge trigger
#    (b) wait until #116 (depends_on enforcement) lands, then merge
#        a sibling edit
#    Recommend (a) for now if the API IAM creds work is needed
#    fast; (b) if we want to fix #116 first.

# 4. Start ADR-0029 (Ralph-loop validation) scoping. Use this
#    handoff's "Three new structural holes" section as the
#    motivating context.
```
