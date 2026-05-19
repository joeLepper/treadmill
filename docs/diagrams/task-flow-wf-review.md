# wf-review — internal flow

The LLM review gate. Runs alongside wf-validate on `pr_opened`/`pr_synchronize`. Verdict feeds the `task_mergeability` VIEW's `review_decision` projection.

```mermaid
flowchart TD
  START([dispatched on pr_opened or pr_synchronize]):::start
  REV[step review<br>role-reviewer<br>sonnet since PR #171]:::flow
  RESULT{verdict}:::dec

  APPROVED([step.completed<br>decision=approved]):::good
  CHANGES([step.completed<br>decision=changes_requested]):::warn
  CRASH([step.failed<br>worker crash]):::bad

  MERGEVIEW[task_mergeability VIEW<br>projects review_decision]:::flow
  FB[wf-feedback dispatched]:::flow
  AUTO_MERGE([gates clean → pr_merged]):::good
  DEAD_CRASH([DEAD-END: 'review-crash-no-retry']):::bad

  START --> REV
  REV --> RESULT
  RESULT --> APPROVED
  RESULT --> CHANGES
  REV -. "worker died" .-> CRASH

  APPROVED --> MERGEVIEW
  MERGEVIEW --> AUTO_MERGE

  CHANGES --> FB
  CRASH --> DEAD_CRASH

  classDef start fill:#cfe,stroke:#393,color:#000
  classDef flow fill:#eef,stroke:#339,color:#000
  classDef dec fill:#ffe,stroke:#993,color:#000
  classDef good fill:#cfc,stroke:#393,color:#000,stroke-width:2
  classDef warn fill:#fec,stroke:#963,color:#000
  classDef bad fill:#fcc,stroke:#933,color:#000,stroke-width:2
```

## Decisions

Per the role-reviewer prompt (bumped to sonnet in PR #171):
- `approved` — diff looks correct. VIEW projects `review_decision='approved'`; auto-merge gate clears.
- `changes_requested` — diff needs work. Dispatches wf-feedback.

(`commented` was a prior state that has been removed — sonnet reviewer is told to pick one of the two terminal verdicts.)

## What dispatches downstream

| wf-review terminal | What fires next |
|---|---|
| `step.completed` decision=approved | nothing; auto-merge predicate picks it up via mergeability VIEW |
| `step.completed` decision=changes_requested | `wf-feedback` (ADR-0029 / task #108 path 1) |
| `step.failed` (no decision) | **nothing** — same scoping as wf-validate; only wf-author has the step.failed → feedback wiring |

## Note on the `review.override` interaction

When `wf-architecture-resolve` emits `verdict=accept-as-is`, the override event flips the mergeability VIEW's `review_decision` projection to `approved` **without** wf-review having to re-run. This is how ADR-0038's deadlock arbitration unblocks merges where the human-ish reviewer (LLM) and the author disagree.
