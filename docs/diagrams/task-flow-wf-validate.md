# wf-validate — internal flow

The deterministic-checks gate. Runs after a PR is opened or synchronized. Verdict feeds the `task_mergeability` VIEW's `validate_decision` projection.

```mermaid
flowchart TD
  START([dispatched on pr_opened or pr_synchronize]):::start
  VAL[step validate<br>role-validator + rule runners]:::flow
  RESULT{rules result}:::dec

  PASS([step.completed<br>decision=pass]):::good
  FAIL([step.completed<br>decision=fail]):::warn
  ERROR([step.completed<br>decision=error]):::warn
  CRASH([step.failed<br>worker crash / no decision]):::bad

  MERGEVIEW[task_mergeability VIEW<br>projects validate_decision]:::flow
  FB[wf-feedback dispatched]:::flow
  AUTO_MERGE([gates clean → pr_merged]):::good
  DEAD_CRASH([DEAD-END: 'validate-crash-no-retry']):::bad

  START --> VAL
  VAL --> RESULT
  RESULT --> PASS
  RESULT --> FAIL
  RESULT --> ERROR
  VAL -. "worker died" .-> CRASH

  PASS --> MERGEVIEW
  MERGEVIEW --> AUTO_MERGE

  FAIL --> FB
  ERROR --> FB

  CRASH --> DEAD_CRASH

  classDef start fill:#cfe,stroke:#393,color:#000
  classDef flow fill:#eef,stroke:#339,color:#000
  classDef dec fill:#ffe,stroke:#993,color:#000
  classDef good fill:#cfc,stroke:#393,color:#000,stroke-width:2
  classDef warn fill:#fec,stroke:#963,color:#000
  classDef bad fill:#fcc,stroke:#933,color:#000,stroke-width:2
```

## Decisions

- `pass` — all rule runners agree. Mergeability VIEW projects `validate_decision='pass'`; auto-merge gate clears.
- `fail` — at least one rule failed. Dispatches wf-feedback.
- `error` — runtime crashed (rule code threw exception). Per ADR-0039, this is **not** a merge-blocker — the validator's own error doesn't gate the merge. But it does dispatch wf-feedback so we learn from it.

## What dispatches downstream

| wf-validate terminal | What fires next |
|---|---|
| `step.completed` decision=pass | nothing; auto-merge predicate picks it up via the mergeability VIEW |
| `step.completed` decision=fail | `wf-feedback` (ADR-0029) |
| `step.completed` decision=error | `wf-feedback` (ADR-0029) |
| `step.failed` (no decision payload) | **nothing** — there's no `maybe_dispatch_feedback_on_step_failed` analog for wf-validate; only wf-author has that |

## Open question this diagram surfaces

`maybe_dispatch_feedback_on_step_failed` is scoped to `workflow_id="wf-author"` only. If a wf-validate worker crashes (silent death rather than completed-with-error), nothing fires. The PR sits with no validate verdict; the mergeability VIEW shows `validate_decision=NULL`; auto-merge never proceeds. Operator-detectable but not auto-recovered.

This was an intentional scoping choice when ADR-0037 was widened to step.failed (PR #152) — the docstring says "extending to other workflows is a one-line change in the caller." Worth deciding whether this should be the default.
