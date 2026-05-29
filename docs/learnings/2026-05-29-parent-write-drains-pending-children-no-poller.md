---
date: 2026-05-29
trigger: correction
status: captured
related: ADR-0007, ADR-0049, ADR-0063
---

# Learning: Parent-write drains pending children — no poller needed

## Trigger

Joe corrected my proposed solution to tonight's Task 3b race (PR #92
`pr_opened`/`pr_merged` recorded with `task_id=NULL`, downstream
`depends_on` stayed blocked, stuck-task-sweep escalated 40 min later,
I had to backfill `task_id` by hand). I first proposed a "read from
event payload" bandaid; he rejected it. I then proposed adopting
RAMJAC's Redis pattern with a "periodic job that drains the
cache." He corrected: "we wouldn't need a periodic job or worker to
check the redis cache. Instead, any time that we are going to update
a task with a PR we check the cache to see if that PR has any
transitions already waiting on it."

## Observation

RAMJAC's actual pattern (sibling Treadmill-managed codebase, private):
when a child write fails its FK lookup, the child caches itself in
Redis keyed by the missing parent's identity. The parent's own
commit handler walks each child-type's pending cache for items
waiting on this parent and drains them inline — multiple child
types each get their own drain step in the same parent commit
transaction. The natural arrival of parent writes IS the
reconciliation cadence; there is no scheduled job and no separate
drain process.

Treadmill already implements this for github webhooks → task_prs per
ADR-0007. The drift is structural: ADR-0049's SQS-based webhook_inbox
ingress skipped the buffer-on-miss call that the HTTP route at
`routers/webhooks.py:223-235` performs. Tonight's race lives entirely
on that drifted ingress path.

## Generalization

When designing eventual-consistency between events that may race FK
parents, our first instinct should be: **find the parent-write site
and drain children there**, not introduce a poller. A poller is the
wrong answer whenever the parent-write happens often enough to be
the natural cadence — which it almost always does in event-sourced
architectures.

A second-order learning: when two ingress paths handle the same event
shape, the eventual-consistency hook is exactly the kind of contract
that drifts. Helper modules that encode the policy aren't enough —
both ingress paths have to call the helper, and there's no compiler
enforcement that they do.

## Proposed rule

Eventual-consistency reconciliation runs in the parent-entity's
commit / insert handler, not in a periodic worker. Any new sweep
proposed to "drain stale state" must justify why the natural
parent-write isn't the right hook before being adopted.

## Proposed remediation

LLM-as-judge: when an ADR or plan proposes a periodic sweep / cron /
poller / background drain process, the judge flags it and asks
whether the parent-write site is the better hook. The judge fires
on both ADR drafts and plan drafts; it never blocks merging but it
surfaces the question.

For the dual-ingress drift specifically: ADR-0063 will encode the
lock-step requirement so any new webhook ingress can't silently
skip the buffer call.

## Notes

ADR-0063 will formalize the pattern Treadmill-wide. The plan that
follows ADR-0063 will fix the SQS dual-ingress drift, generalize the
key shape beyond `(repo, pr_number)`, and add a contract-enforcement
check.

Related: [[ADR-0007]] (current cache-then-heal implementation),
[[ADR-0049]] (the App-identity ingress cutover that introduced the
drift), [[ADR-0035]] (scheduler — where I'd been wrong to reach for
a tick-based drain).
