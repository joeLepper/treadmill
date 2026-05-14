# Session handoff — 2026-05-13 (ADR-0024 activation + Ralph-loop scoping signal)

## What we tried

After commit 8a52c17 (the two fixes for the ADR-0023 smoke holes —
consumer redispatch on pr_merged + wf-feedback empty-diff
softening), the goal was: bring the stack back up, fire fresh
work, validate the round-trip end-to-end.

Three sequential cycles in this session:

1. **Re-fire ADR-0023 plan** (PR #21). Task 1 ("Provision API IAM
   user + secret via CDK") wf-author failed with empty-diff — the
   work was already merged into main by the prior smoke's PR #20.
   Surfaced a new hole: **wf-author can't distinguish "I was
   supposed to do work" from "the work is already done"**.

2. **Activate ADR-0024 plan** (PR #22) for genuinely fresh work
   (deploy-events queue + watcher subprocess). The plan-merge
   trigger fired, 5 tasks materialized, task 1 (CDK construct)
   dispatched. SSO TTL bit us mid-cycle — both poll loops fail-
   looping on ExpiredToken. Recovered with `aws sso login` +
   `treadmill-local redeploy`. Task 1 wf-author succeeded
   (substantive 200-line CDK construct + tests). wf-review came
   back **approved** via JSON envelope (the second clean
   JSON-envelope verdict this session).

3. **PR #23 merge** (the task 1 PR) was the round-trip validation
   point. Tests on the branch failed under CDK token semantics
   (third instance of the same hallucinated-API bug class). After
   manual fix-up + rebase + force-push, PR #23 squash-merged.
   The reevaluate-on-pr_merged fix fired correctly but found
   zero candidates because `task_status` projects deferred runs
   as `wf-author: executing` rather than `registered` — yet
   another structural gap.

## The 16-commit infrastructure tally still holds

Across this whole session, every piece of new infrastructure
shipped this session **works correctly** in the cases the unit
tests cover:

| Capability | Validated against |
| --- | --- |
| Plan-merge trigger (ADR-0021) | PR #19, #21, #22 all fired correctly |
| #108 path 1 — `gh pr comment` | PR #20, #23 — clean comments posted |
| ADR-0027 JSON envelope | PR #20 (request_changes), PR #23 (approve) — both parsed clean, rationale populated |
| #108 wf-feedback self-trigger | PR #20's review fired wf-feedback via `review-run=` dedup namespace |
| ADR-0028 auto-seed | Every redeploy this session populated the JSON-envelope prompt |
| ADR-0028 `treadmill role show/update/versions` CLI | Validated mid-Phase-2 |
| ADR-0023 (CDK side) | PR #20 landed the API IAM user + bounded policy + secret |
| ADR-0024 (CDK side) | PR #23 landed the deploy-events queue + SNS subscription + filter policy |
| ADR-0026 dedup | Multiple namespaces (wf-review, wf-feedback review-run=) clean |
| `wf-validate` runs in the wild | First-ever wf-validate run fired on PR #23 via the pr_synchronize fan-out (placeholder per ADR-0022; produces "placeholder" decision) |

The Ralph-loop **content** layer is what's broken. The
infrastructure is solid.

## The five structural holes ADR-0029 (#101) needs to absorb

The ADR-0023 smoke surfaced two; this session surfaced three more.
All five are exactly the class of bug a Ralph-loop validation gate
is supposed to catch.

### Hole 1: hallucinated APIs (THREE instances in this session)

`wf-author` produces plausible-looking imports / package names /
API calls that don't actually exist:

* **PR #18** (o11y SDK, prior session): requested
  `opentelemetry-instrumentation-boto3>=0.41b0` — does not exist
  on PyPI. Correct name: `-botocore`. `uv lock` would have
  failed. Caught only at `treadmill-local up` time.
* **PR #20** (API IAM user, this session): the test wf-author
  produced used `policy.Resource` as `set[str]` — but CDK
  synthesizes cross-resource ARN refs as `Fn::Sub` / `Fn::Join`
  / `Fn::GetAtt` **dicts**, so the test crashed with
  `TypeError: unhashable type: 'dict'`. `pytest` would have
  failed.
* **PR #23** (deploy-events CDK, this session):
  `aws_cdk.aws_sns_subscriptions.SubscriptionFilter` — does not
  exist. Correct: `aws_cdk.aws_sns.SubscriptionFilter`. `cdk
  synth` would have failed.

**Same class three times** in two sessions. wf-review approved
all three because the diff matches the spec semantically.
Approval is on text, not on execution. A `pytest <task scope.files
test paths>` deterministic check would have caught all three.

This is the **canonical case** for ADR-0029's deterministic-check
runner.

### Hole 2: empty-diff failure when work is already done

`wf-author` raises `CodeAuthorError` on empty diff. For
**originating** workflows (the first time the work is done) that's
correct. But when a re-fire happens against a tree where the work
is already merged (PR #21's task 1 re-fire), `wf-author` looks,
sees the work is done, makes no diff → hard fail. The runner has
no way to recognize "the task's stated outcome already obtains."

**Possible shapes:**

* A wf-author empty-diff soft path mirroring wf-feedback's
  `responded-without-change` (Joe leaned against this earlier —
  "if the author was supposed to do work and didn't, that IS a
  failure"). Reasonable position; the trade-off is keeping the
  re-fire scenario broken.
* A pre-author scope check: "do scope.files contain the patterns
  named in scope/intent" — semantic, brittle, judgement-heavy.
* A post-author validation that says "did the task's outcome
  obtain" via an llm-judge gate. This is the Ralph-loop framing.

**Joe's call 2026-05-13**: this state is rare, "pay attention to
see if we run into it again in the future." Not fixing now.

### Hole 3: wf-feedback empty-diff convergence is a one-shot

The fix in 8a52c17 (wf-feedback emit `responded-without-change`
instead of raising) keeps the run from crashing, but the loop
doesn't converge. The PR stays in `blocked-on-review` because
wf-review's verdict was `changes_requested`. The operator has to
intervene to merge.

This was an explicit trade-off: better-than-crash. The full
convergence needs a validation gate that says "the reviewer's nit
isn't blocking" or "the analyzer's directive is now satisfied."
Ralph-loop territory.

### Hole 4: deferred-run dispatch isn't picked up by reevaluate

The dispatcher's deferred-dispatch path creates `WorkflowRun` +
`WorkflowRunStep` rows in `pending` state when a task's
dependencies are unsatisfied. **The runs exist but no
step.ready event is published.** When a `pr_merged` event later
satisfies the dependency, `reevaluate` runs but filters on
`task_status.derived_status = 'registered'`. Tasks with deferred
runs are projected as `executing` (the workflow_run row makes the
task look "in flight"), not `registered`, so reevaluate skips
them. They stay deferred forever.

The fix needed: when a `pr_merged` event satisfies a dep on a task
with a deferred (pending, no step.ready) run, **emit step.ready
for the existing run** rather than creating a new one. The
existing `_has_step_ready_event` idempotency probe in
`dispatch_task` already returns the run_id for "already
dispatched" cases; the symmetric "exists but not yet emitted"
case is what needs adding.

**This is the actual gap** I should have fixed in 8a52c17 but
mis-diagnosed. The redispatch-on-pr_merged fire is correct; it
just has no useful work to do because reevaluate's selector
misses these tasks.

### Hole 5: wf-author generates tests that the loop never runs

The three hole-1 instances all involve **tests that wf-author
generated** — `test_secrets_construct`, `test_cloud_lite_stack`,
`test_api_iam_policy_has_required_statements`. wf-author wrote
plausible-looking assertions; wf-review evaluated the *content*
against the spec; no one ran the tests. This is the exact gap
ADR-0029's `kind=deterministic` runner is for. A single
`uv run pytest <scope>` per task would have caught all three.

## What ADR-0029 needs to scope (concrete from observed failures)

Take these as the requirements the next ADR should answer:

1. **A worker disposition for `kind='deterministic'`.** Reads
   the task's `task_validations` rows (already persisted from
   plan-doc parse), executes scripts via subprocess, maps
   exit-code to ADR-0012 decision values. Not a Claude session.
2. **A worker disposition for `kind='llm-judge'`.** Spawns
   Claude Code with the check's prompt as the task spec; parses
   the JSON envelope verdict (reuses ADR-0027's shape); maps to
   decision.
3. **A built-in `pytest` validation** that fires for every task
   whose `scope.files` includes test paths or implies them
   (heuristic: any `*.py` change in a project with `tests/`).
   This addresses hole 1 + hole 5 directly.
4. **GitHub check_run posting** so the verdict surfaces on the
   PR page without a CLI query.
5. **A retry/cap policy** mirroring `wf-ci-fix` + `wf-conflict`.
6. **Deferred-run dispatch from reevaluate** (hole 4). Strictly
   speaking this is ADR-0021 territory not ADR-0029, but the
   redispatch / validation paths interact — the validation gate
   may fire on tasks whose runs were created by deferred
   dispatch, and the trigger flow needs to route correctly.
7. **The relationship to wf-feedback's empty-diff path** (hole
   3): if validation says "the change isn't needed" the
   feedback loop can converge with the `responded-without-change`
   decision rather than leaving the PR blocked.
8. **Empty-diff in wf-author** (hole 2): not addressed by
   ADR-0029 directly; revisit if it bites in practice.

## Current PR / branch state

* **Main branch:** at commit f22799a + the PR #23 squash merge
  on top. Tests pass: 36/36 in `infra/`.
* **Open PRs:** none (PR #23 merged with the manual fix-up).
* **Deferred work that didn't dispatch this session:**
  - ADR-0024 tasks 2-5 (treadmill-local init for deploy-events,
    deploy-watcher module, spawn-on-up, runbook). All have
    workflow_run rows from the deferred-dispatch path; need
    hole-4's fix OR manual re-dispatch to advance.
  - ADR-0023 tasks 2-4 from the CLI-submitted plan
    (`a5d0a5fb-…`). Same situation; their task_dependencies
    were dropped manually so the deferred runs would dispatch
    cleanly after hole-4's fix.
* **Plan files:** both ADR-0023 + ADR-0024 plans are `active`.
* **Stack:** torn down (`treadmill-local down --deployment personal`).

## Commits this session (running total)

```
117d520  Phase 0: ADRs Accepted + plans Active + sequencing doc
65022aa  Phase 1: #108 path 1 (gh pr comment + self-trigger)
e942251  Phase 2a: role_versions table + ORM
acaec57  Phase 2b: PATCH + GET versions endpoints
bef0169  Phase 2c: seed --reset-prompts-from-code
11474ad  Phase 2d: treadmill role show/update/versions CLI
e34144c  Phase 2e: auto-seed on first API startup
b87d340  Phase 2f: operator runbook + post-mortem
fa0f82c  Phase 3: JSON envelope parser + prompt rewrite
b6ae461  Phase 3 plan post-mortem
b7c21b4  Phase 4 smoke (squash-merge)
d629ebd  PR #18: Wire OTel SDK (squash-merge)
b71c023  Hotfix: opentelemetry-instrumentation-boto3 → -botocore
1367a4e  Phase 4 smoke complete + sequencing-plan completed
d054b3e  Handoff: Phase 4 smoke + validation holes
5f06478  Activate ADR-0023 + flip plan to active (PR #19 squash-merge)
331804d  PR #20 squash-merge: Provision API IAM user + secret via CDK
07d3455  PR #21 squash-merge: Re-fire ADR-0023 plan after fixes
8a52c17  Fix the two validation gaps the ADR-0023 smoke surfaced
4762d4d  Activate ADR-0024 + flip plan to active (PR #22 squash-merge)
f22799a  Fix test_resource_count_is_minimal — secrets count is 4
df1b275  PR #23 squash-merge: Provision deploy-events SQS + SNS via CDK
```

## Lessons captured into the file system this session

* `docs/plans/2026-05-13-in-session-sequencing.md` — Phase 0-4 coordination + post-mortem (completed)
* `docs/plans/2026-05-13-structured-review-envelope.md` — ADR-0027 plan (completed)
* `docs/plans/2026-05-13-db-authoritative-configs.md` — ADR-0028 plan (completed)
* `docs/plans/2026-05-12-api-credentials-iam-user.md` — ADR-0023 plan (active, task 1 merged via PR #20)
* `docs/plans/2026-05-12-auto-redeploy-watcher.md` — ADR-0024 plan (active, task 1 merged via PR #23)
* `docs/runbooks/edit-a-role-prompt.md` — first runbook in the new directory (ADR-0028)
* `docs/handoffs/2026-05-12-loop-hardening-and-first-smoke.md` — prior session
* `docs/handoffs/2026-05-13-adr-0023-smoke-and-validation-holes.md` — earlier this session
* `docs/handoffs/2026-05-13-ralph-loop-scoping-signal.md` — this doc

## Pending task list at session close

* #95 Bootstrap non-Treadmilled repos
* #96 Learnings-to-validations pipeline → ADR-0030 (after ADR-0029)
* #98 Observability stack (1/5 tasks merged; 4 pending)
* **#101 Ralph-loop validation architecture → ADR-0029 (next, with the 5 holes above as scoping signal)**
* #103 Structured step-output parsing ADR
* #104 API credentials long-lived IAM-User keys (CDK side merged via PR #20; tasks 2-4 stuck on hole 4 + need post-deploy `aws iam create-access-key` + `put-secret-value`)
* #107 docs/runbooks/ reorganization
* #109 Treadmill as a real GitHub App (future cleanup)
* #114 Delete VERDICT regex tourniquet (run count now: 2 + 1 second wf-review on PR #23 = 3 clean of 10 needed — still 7 to go)
* #116 Re-verify depends_on enforcement (now better-framed as "hole 4: deferred-run dispatch isn't picked up by reevaluate")

## Resume next session

```bash
# 1. Bring the stack up:
cd /home/joe/treadmill/tools/local-adapter
uv run treadmill-local up --deployment personal

# 2. ADR-0029 (Ralph-loop validation) scoping. Use this handoff's
#    "Five structural holes" + "What ADR-0029 needs to scope"
#    sections as the motivating context. Likely shape:
#    docs/adrs/0029-ralph-loop-validation-runner.md.

# 3. If you want the deferred tasks to dispatch instead of
#    starting fresh: that's hole 4. Either fix it
#    (modify reevaluate to handle deferred runs) or manually
#    publish step.ready events for the pending runs. Both are
#    ad-hoc; better to teach reevaluate the right behavior.

# 4. The `aws iam create-access-key --user-name
#    treadmill-personal-api` post-deploy step is still owed once
#    we want the API to actually use the long-lived creds. Until
#    then, the API still runs on operator SSO and we'll keep
#    hitting the 1h TTL.
```
