# ADR-0021: Plan-merge-to-main as the submission trigger

- **Status:** proposed
- **Date:** 2026-05-12
- **Related:** ADR-0007, ADR-0010, ADR-0011, ADR-0017

## Context

Today, `treadmill plan submit --doc <path>` is the only way to get a plan into Treadmill's execution path. The CLI POSTs the doc to the API; the API parses, spawns tasks, dispatches. Phase 2's smoke proved this works end-to-end. But there's a friction worth removing: **the CLI is now the bottleneck for a workflow that wants to be source-controlled.**

Today's flow:

1. Operator (or agent) writes a plan as `docs/plans/2026-XX-XX-<thing>.md`.
2. Operator opens a PR with the doc.
3. PR gets reviewed.
4. PR merges to main.
5. Operator separately runs `treadmill plan submit --doc docs/plans/2026-XX-XX-<thing>.md --repo joeLepper/treadmill`.

The fifth step is a redundant manual action. The merge to `main` is *already* the operator's "yes, do this" signal — the same review/approval discipline that any code change goes through. Treadmill should detect the merge and start implementing automatically. **The merge itself is the submission.**

This becomes load-bearing as Treadmill moves toward "Treadmill builds Treadmill" (operator's stated direction). The natural agent loop is: a wf-plan task drafts a plan doc as a PR, an operator (or another agent's review pass) approves the PR, the merge triggers wf-author + downstream workflows. CLI submission only makes sense as a backstop for cases where the operator wants to bypass review (which should be rare and conscious).

ADR-0017 wired the GitHub webhook ingestion path; the pieces are in place. We need: (a) a Treadmill verb for "a plan doc got merged," (b) a normalizer rule, (c) a trigger handler that fetches the doc + creates the plan.

## Decision

### Trigger event: `pull_request:closed:merged` filtered by changed files

Subscribe the GitHub webhook to `pull_request` events (already done — the smoke webhook lists `pull_request`, `pull_request_review`, `check_run`). When the poller dequeues a `pull_request` event with `action=closed` and `pull_request.merged=true`, the normalizer inspects the merged PR's changed files. If any changed file matches the plan-doc path pattern, emit a new Treadmill verb: **`plan_doc_merged`**.

The choice of `pull_request:closed:merged` over `push` to main:

- `push` is simpler (one event, no PR metadata) but fires for direct commits to main too. Allowing direct-to-main bypass is *worse* discipline than gating on PR review.
- `pull_request:closed:merged` carries the PR number + reviewer info + merge SHA — useful for audit + provenance.
- Existing webhook subscription already covers `pull_request` events; no GitHub-side change.

### Plan-doc path pattern: `docs/plans/*.md`

The pattern is hard-coded in the normalizer at v0. Conventions:

- Plan docs live in `docs/plans/`. ADR-0010's plan-rooted task hierarchy implicitly assumes this directory; we make it explicit.
- A merge that touches `docs/plans/*.md` (any glob match) triggers one `plan_doc_merged` event per file touched.
- Other files in the same PR (code, ADRs, tests) don't trigger anything — only the plan-doc paths do.

Repo-level overrides (e.g., a repo that keeps plans in `roadmap/`) are out of scope at v0. When task #95 (bootstrap non-Treadmilled repos) lands, the path pattern becomes per-repo config.

### Frontmatter marker for activation: `status: active`

A plan doc carries frontmatter:

```yaml
---
status: active
trigger: <human-readable reason>
parent: <optional path to parent plan>
---
```

When the merge handler fetches the doc, it parses the frontmatter. If `status != "active"`, the merge is observed (event row written) but **no plan/task creation fires**. This lets the operator merge a `status: drafting` plan for review-purposes-only, or a `status: completed` post-mortem, without spuriously triggering execution.

The existing `docs/plans/2026-05-13-week-4-dev-local-deployment.md` already has this frontmatter shape — no migration needed.

### Plan identity: deterministic from repo + path + merge SHA

```python
plan_id = uuid.uuid5(
    uuid.NAMESPACE_OID,
    f"{repo}:{path}@{merge_commit_sha}",
)
```

Properties:

- **Idempotent**: SQS redelivery, webhook replay, or the operator running `treadmill plan submit --doc` against the same merged commit all converge on the same `plan_id`. The existing `ON CONFLICT (id) DO NOTHING` on the `plans` table handles dedup.
- **Distinct across merges**: if the operator edits the doc and re-merges (rare), each merge gets a distinct commit SHA → distinct `plan_id` → distinct Plan row. That's the desired semantic: "merge = submission" means re-merging is re-submitting.
- **No schema change** to the existing Plan model.

### Handler: trigger evaluator fetches doc + reuses the Scenario-1 path

When the consumer projects `plan_doc_merged`, the trigger evaluator (existing component, ADR-0011) handles it. Steps:

1. Read the event payload: `{repo, path, merge_commit_sha, pr_number, author}`.
2. Fetch the doc content from GitHub: `gh api /repos/<owner>/<repo>/contents/<path>?ref=<merge_commit_sha>` (or `https://raw.githubusercontent.com/.../...`). Use the existing API-side `httpx.AsyncClient` (`github_client`).
3. Parse the frontmatter. If `status != "active"`, persist a `plan_doc.observed_inactive` event and return (no dispatch).
4. If `status == "active"`, call the existing internal plan-creation function — the same code path that backs `POST /plans` with `doc_content`. This reuses `parse_plan_doc`, task spawning, `dispatcher.persist_and_publish(PlanRegistered)`, `PlanActivated`, and per-task `dispatch_task`. No new dispatch machinery.

The merge-driven path is **a different *trigger* into the same execution machinery** as the CLI path. The Plan row, task rows, run rows, dispatch — all identical to what `treadmill plan submit --doc` produces.

### Failure path: malformed plan doc post-merge

If `parse_plan_doc` raises after the merge happened:

- Persist an event: `entity_type=plan_doc, action=parse_failed, payload={repo, path, merge_commit_sha, error, error_type}`.
- The error becomes visible via the observability stack (ADR-0020) and via `SELECT * FROM events WHERE action='parse_failed'`.
- No automatic remediation at v0. Operator notices, edits the plan, re-merges.

A future improvement (banked, not in this ADR): the handler files a GitHub issue on the repo pointing at the merge commit + the parse error. Adds operator-noticed-ness without operator-cursor-required.

### CLI submission survives as a backstop

`treadmill plan submit --doc <path>` continues to work. Use cases that survive:

- **Plans not in source control**: e.g., experimental local plans the operator doesn't want to commit. Submit via CLI, never merge to main.
- **Plans submitted before the trigger lands**: existing plans in the repo (like the Week-4 plan) won't auto-trigger because they merged before this ADR. The operator can still use the CLI to dispatch them.
- **Debugging / manual override**: when the auto-trigger misbehaves, the CLI is the operator's escape hatch.

The CLI path's `--dev` fast-path (intent-only-in-fully-local) is unchanged.

### Per-repo enablement

The trigger only fires for repos Treadmill is configured to manage. At v0 with one deployment + one repo (joeLepper/treadmill), this is implicit — there's only one repo whose webhooks reach the API.

When task #95 (bootstrap non-Treadmilled repos) lands, "is this repo authorized for plan-merge dispatch?" becomes a real config check. The handler should consult a per-repo allow-list. Out of scope here.

## Bunkhouse precedent

Bunkhouse uses GitHub webhooks for PR-state events but doesn't drive *plan submission* from merges — bunkhouse's "what to do next" is determined by the orchestrator process the operator runs interactively. So this ADR has no direct bunkhouse analog. It's a genuinely new path that Treadmill takes specifically because Treadmill's execution unit is the Plan, not the conversation. Worth flagging because the "look at bunkhouse first" learning would have produced an empty answer here.

## Trade-offs

- **The merge is irreversible.** Once a plan-doc with `status: active` merges, Treadmill starts executing. If the operator wants to abort, they have to merge a follow-up commit that flips `status` or removes the file — by which point one or more tasks may already be dispatched. Mitigation: rely on PR review as the gate; operators don't merge plan docs casually. The autoscaler's `max` cap is the bound on damage.
- **A merge with `status: active` + parse failure is a silent-ish hole at v0.** The error lands in the events table but the operator has to look. Mitigation: observability stack (ADR-0020) surfaces the event row; future GitHub-issue-filing closes the loop. At single-operator personal scale, the operator's already-watching-Grafana frequency is high.
- **GitHub API rate-limiting on doc fetch.** Each merge handler call hits `gh api /repos/.../contents/...`. At GitHub's 5000-req/hour PAT limit, this is a non-issue for normal volume. If we ever batch-merge many plans, may need consideration. Not v0.
- **Re-merge → re-submission is "expected, sometimes surprising."** An operator who reverts then re-merges the same plan gets two Plan rows. Documented in the ADR; flag for operators.

## Alternatives considered

- **Trigger on `push` to main, not `pull_request:closed:merged`.** Simpler event, but allows direct-to-main bypass of PR review. Rejected: the PR-gated discipline is the entire reason we want this trigger in the first place.
- **Trigger on a specific PR label** (e.g., `plan:execute`). Adds friction (operator must remember to label) without security gain over the merge-itself signal. Rejected.
- **Trigger on a commit-message convention** (e.g., subject starts with `plan:`). Brittle; an operator who rewrites the subject in the merge UI loses the trigger. Rejected.
- **Run a daemon that periodically scans `docs/plans/` for new active plans.** Eventually consistent; works without webhooks. But adds polling cost + duplicates webhook machinery. Rejected.
- **Add a `treadmill plan submit-from-merge` CLI** that the operator runs manually post-merge. Reverts to today's friction. Rejected.
- **Embed a "Treadmill, please execute this" frontmatter field** instead of `status: active`. Functionally identical; `status: active` is more general (the same field tracks plan lifecycle) and reuses an existing convention. Prefer.

## Open questions

- **Q21.a — Should the trigger also fire on `pull_request:opened` for plan docs in draft?** This would let a wf-plan-draft workflow author a plan and have Treadmill auto-respond (e.g., by validating the doc shape, posting feedback). Out of scope at v0; bank as a future enhancement after ADR-0021's basic path proves out.
- **Q21.b — What if a single merge touches multiple plan docs?** v0 fires one event per file; each is processed independently. If `status: active` plans depend on each other (e.g., parent/child), the per-Plan-row machinery doesn't coordinate them. Operator's responsibility to order merges. Future: a `depends_on:` frontmatter field that the handler honors.
- **Q21.c — Does an inactive-status merge leave a record?** Yes — the handler still writes a `plan_doc.observed_inactive` event so the operator can see "Treadmill saw the merge and chose not to dispatch." Useful for debugging "why didn't my plan run?"
- **Q21.d — What about plan docs in subdirectories?** `docs/plans/2026-05-13/sub-thing.md`? v0 globs `docs/plans/*.md` (one level). Bump to `docs/plans/**/*.md` if we discover plans nest. Easy fix.
- **Q21.e — Should the merge author become the plan's `created_by`?** Today's CLI plumbs `--created-by` through. The webhook payload includes the PR author + the merge committer. Use the PR author as `created_by` (semantically: "who proposed this plan"). Document the choice; revisit if it surfaces ambiguity.

## Consequences

- **New verb in the normalizer**: `services/api/treadmill_api/webhooks/normalize.py` gains a case that maps `pull_request:closed` + `merged=true` + file-path-filter to `plan_doc_merged`. Multiple files → multiple events.
- **New trigger handler**: the consumer (or a sibling trigger-evaluator module) gains a handler for `plan_doc_merged`. The handler is the only meaningful new code path; everything downstream (parsing, plan creation, task dispatch) is unchanged.
- **GitHub doc fetch**: the handler calls the existing `httpx.AsyncClient` against `https://raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>` (or `gh api`). The client already exists for ADR-0013's conflict-detection sweep.
- **No schema change.** Plan model is unchanged. The plan-id derivation is application-level.
- **`event_triggers` seed**: a new row `(event_type='plan_doc_merged', workflow_id='wf-author')` or similar — but actually, the merge handler doesn't go through `event_triggers`; it directly creates a Plan + spawns tasks per the plan doc. So `event_triggers` doesn't need a new row at v0. (The `event_triggers` table is for "events that should auto-spawn a workflow run"; plan_doc_merged spawns *the parsed plan's tasks*, which is a different machinery.)
- **Test surface**: integration test against moto + a synthetic merge event with a synthetic plan doc. Asserts: (a) Plan + tasks created, (b) dispatch fires, (c) parse failure persists a `parse_failed` event, (d) inactive status persists an `observed_inactive` event.
- **Tracks task #95** (bootstrap non-Treadmilled repos): this ADR assumes one repo (Treadmill's own). When the second repo joins, the handler's "is this repo authorized" check becomes load-bearing.
- **Test the failure mode in CI**: a malformed plan doc merging to main should NOT crash the consumer (the existing exception-handling pattern in the consumer should absorb it; verify).
