# wf-ci-fix — internal flow

The CI-failure recovery workflow. Dispatched on `check_run.completed=failure` webhook events when CI fails on a PR.

```mermaid
flowchart TD
  START([dispatched on check_run.completed=failure]):::start
  ANALYZE[step analyzer<br>role-ci-analyzer]:::flow
  ACTION[step action<br>role-code-author with analysis]:::flow
  STAGED{diff non-empty?}:::dec

  WROTE([step.completed<br>decision=pass<br>commit + push triggers CI re-run]):::good
  FAIL([step.completed<br>decision=fail<br>can't fix this CI failure]):::warn
  CRASH([step.failed]):::bad

  PR_SYNC([CI re-runs on new HEAD]):::ext
  CAP{wf-ci-fix attempts < 3?}:::dec
  RETRY[wf-ci-fix re-dispatches]:::flow
  DEAD_CAP([DEAD-END: 'ci-fix-cap-reached']):::bad
  DEAD_FAIL([DEAD-END: 'ci-fix-gave-up']):::bad

  START --> ANALYZE
  ANALYZE --> ACTION
  ACTION --> STAGED
  STAGED -- non-empty --> WROTE
  STAGED -- empty --> FAIL
  ACTION -. crash .-> CRASH

  WROTE --> PR_SYNC
  PR_SYNC -. "CI fails again" .-> CAP
  CAP -- yes --> RETRY
  CAP -- no --> DEAD_CAP

  FAIL --> DEAD_FAIL
  CRASH --> DEAD_CAP

  classDef start fill:#cfe,stroke:#393,color:#000
  classDef flow fill:#eef,stroke:#339,color:#000
  classDef dec fill:#ffe,stroke:#993,color:#000
  classDef good fill:#cfc,stroke:#393,color:#000,stroke-width:2
  classDef warn fill:#fec,stroke:#963,color:#000
  classDef bad fill:#fcc,stroke:#933,color:#000,stroke-width:2
  classDef ext fill:#ffd,stroke:#993,color:#000
```

## Cap

`wf-ci-fix` is capped at 3 attempts per task (`CI_FIX_MAX_ATTEMPTS=3`). Once the cap is hit, no further attempts are made even if CI continues to fail. The PR sits with red CI; auto-merge gate stays blocked.

## What dispatches downstream

| wf-ci-fix terminal | What fires next |
|---|---|
| `step.completed` decision=pass with diff | nothing automatic; the push triggers a CI re-run, which on completion may dispatch a fresh wf-ci-fix via the github webhook path |
| `step.completed` decision=fail (analyzer/action gave up) | **nothing** |
| `step.failed` (crash) | **nothing** — no step.failed wiring for wf-ci-fix |

## Note on the dead-end classes

Both `ci-fix-cap-reached` and `ci-fix-gave-up` are PRs where the diff is otherwise mergeable but CI is red. Today these surface only as operator-detectable state (the PR shows red CI; mergeability VIEW shows `ci_conclusion='failure'`). No notification fires. The 2026-05-19 audit found 2 tasks in this terminal state (both from old hands-free-driving plans — possibly abandonable).
