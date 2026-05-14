---
name: dual-ingress-paths-need-a-shared-facade
date: 2026-05-14
status: open
trigger: ADR-0029 smoke 2026-05-14 — task 2 of the plan never dispatched
  because every github ``pr_merged`` event came in with ``task_id=NULL``.
  Root cause: two webhook ingress paths (HTTP at ``routers/webhooks.py``
  and SQS poller at ``coordination/webhook_inbox.py``) drifted on the
  ``task_id`` field. The HTTP route did the ``task_prs (repo, pr_number)``
  lookup before persisting; the SQS poller didn't. Same DB target, same
  normalizer, same publisher — diverged on the one field that the
  dependency-gate query keys on.
session_id: 2ccc6390-0915-4884-8fc8-86692b71895d
captured_via: hook (correction-phrase "we don't want")
---

## The lesson

When two code paths persist to the same table, they need a **single
persistence facade** — not "two callers that each do roughly the same
thing." The drift is silent because each path is correct on its own;
the bug only surfaces when downstream code keys on the field that one
side forgot to set.

This is the third instance of the same class of bug in this multi-day
push:

1. **PR #18**: code-side pyproject.toml + worker code-side both
   referenced `opentelemetry-instrumentation-boto3` (which doesn't
   exist on PyPI). Both sides looked consistent because they
   referenced the same wrong name.

2. **PR #20**: `test_secrets_construct.py::test_exactly_four_secrets`
   was updated when the new IAM-user secret landed; the parallel
   `test_cloud_lite_stack.py::test_resource_count_is_minimal` (which
   asserts the **same invariant** at a different layer) was NOT
   updated.

3. **2026-05-14 ADR-0029 smoke**: the HTTP webhook route + the SQS
   poller persist the same kind of row to ``events``. The HTTP route
   stamps ``task_id``; the SQS poller didn't. The dependency-gate
   query (``dispatch._is_dep_pr_merged``) reads ``events.task_id``
   directly. Silent gap.

The unifying pattern: **near-parallel code paths that share a
target but lack a shared facade drift**. Either path looks right at
review time. The bug only manifests under specific downstream
conditions.

## What to do

### Immediate (when you see this pattern)

When you find yourself authoring or reviewing code that "matches
the shape of the other path," stop and ask whether the two paths
should share a single function/facade/abstraction instead. The
duplication is the bug; the eventual drift is the symptom.

### Structural fix

Refactor toward a single persistence facade. For the webhook-event
case specifically:

```python
# coordination/event_persistence.py (proposed)
async def persist_github_event(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    normalized: NormalizationResult,
    body: dict[str, Any],
) -> Event:
    """Single seam for github-event persistence. Used by both the HTTP
    webhook endpoint and the SQS webhook-inbox poller.

    Stamps ``task_id`` via the ``task_prs`` bridge, populates
    ``commit_sha`` via ``_extract_commit_sha``, writes the row idempotently
    on ``event_id``, and returns the canonical Event.
    """
```

Both ``routers/webhooks.py`` and ``coordination/webhook_inbox.py``
import this and stop carrying their own logic. Tracked as task
#117.

### Programmatic enforcement

A Ralph-loop validation rule (per ADR-0029) that runs an llm-judge
check across PRs touching "near-parallel" code paths would catch
this class at PR-time. Prompt template:

> Inspect the diff for new code paths that persist to or query a
> shared DB target. If the new path looks like a duplication of an
> existing path (same target table, similar shape), check that
> every field the original path sets is also set here. Surface any
> field-level divergence as `fail`.

That's the load-bearing prompt this learning suggests adding to
the starter rules in `docs/knowledge-base/rules/`.

## Patches applied in this learning's cycle

- Fix to `coordination/webhook_inbox.py` adding the `task_prs`
  lookup. Symptom-level. Committed [hash to be added once landed].
- Task #117 ("Unify webhook-event persistence behind a single
  facade") tracks the structural cleanup.

## Related

- [[feedback_systematic_audit]] in auto-memory — same family of
  "one symptom usually has structural neighbors; audit broadly."
  This learning extends it: dual-paths-sharing-state is the
  specific structural neighborhood to audit when you see field
  drift.
- ADR-0029 (Ralph-loop validation runner) — the long-term
  enforcement vehicle.
- ADR-0011 (event-driven immutable runtime architecture) — the
  ADR that established the events table as the audit log of
  record; this learning argues that any path WRITING to that
  audit log needs a single facade, not parallel implementations.
