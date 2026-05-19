# Task lifecycle: ADR → merged main

End-to-end render of how a unit of work moves through Treadmill, from an accepted ADR to a squash-merged commit on `main`. The diagram is a composition of the decisions captured in:

- [ADR-0031](../adrs/0031-auto-merge-as-completion-trigger.md) — auto-merge as completion trigger
- [ADR-0032](../adrs/0032-role-architect-and-documentarian.md) — role-architect verdicts
- [ADR-0038](../adrs/0038-ralph-loop-deadlock-arbitration.md) — deadlock arbitration via architect
- [ADR-0040](../adrs/0040-architect-tunes-validator-on-accept-as-is.md) — architect tunes validator on accept-as-is
- [ADR-0042](../adrs/0042-validate-override-channel.md) — `validate.override` channel

The diagram is the contract these ADRs compose into. If implementation diverges from the diagram, either the implementation is wrong or one of the ADRs needs amending.

```mermaid
sequenceDiagram
    actor Operator
    participant ADR
    participant Plan
    participant Task
    participant Author as wf-author
    participant Validate as wf-validate
    participant Review as wf-review
    participant Feedback as wf-feedback
    participant Architect as wf-architecture-resolve
    participant Consumer as coordination<br/>consumer
    participant MergeView as task_mergeability<br/>VIEW
    participant AutoMerge as auto-merge<br/>trigger
    participant GitHub as GitHub PR
    participant Main as main

    Operator->>ADR: /decide → accepted
    ADR->>Plan: /plan → activated
    Plan->>Task: tasks registered
    Task->>Author: dispatch wf-author

    Author->>GitHub: push + gh pr create
    GitHub->>Consumer: pr_opened
    Consumer->>Validate: dispatch
    Consumer->>Review: dispatch

    par
        Validate->>GitHub: run rule checks
        Validate->>Consumer: step.completed
    and
        Review->>GitHub: post review
        Review->>Consumer: step.completed
    end

    alt clean: validate.pass + review.approved
        Consumer->>MergeView: read mergeability
        MergeView-->>Consumer: mergeable
        Consumer->>AutoMerge: set 30s deadline (Redis)
        AutoMerge->>GitHub: PUT /pulls/{n}/merge
        GitHub->>Main: squash merge
    else validate-fail deadlock (ADR-0038)
        Validate->>Consumer: step.completed(decision=fail)
        Consumer->>Feedback: dispatch
        Feedback->>Consumer: step.completed(decision=responded-without-change OR fail)
        Consumer->>Architect: dispatch wf-architecture-resolve

        alt architect=accept-as-is (ADR-0038, ADR-0040, ADR-0042)
            Architect->>Consumer: step.completed(dispatch.review_override+validate_override)
            Consumer->>Consumer: INSERT review.override
            Consumer->>Consumer: INSERT validate.override
            Note over Consumer: session.flush() required between INSERT and VIEW read<br/>(see learning 2026-05-17-auto-merge-trigger-loses-race-with-validate-override)
            Consumer->>MergeView: read mergeability
            alt VIEW sees override
                MergeView-->>Consumer: mergeable
                Consumer->>AutoMerge: set 30s deadline
                AutoMerge->>GitHub: PUT /pulls/{n}/merge
                GitHub->>Main: squash merge
            else VIEW snapshot pre-write (lost race)
                MergeView-->>Consumer: blocked-on-validate
                Note over Consumer: silent logger.debug bailout — PR sits MERGEABLE/CLEAN forever
            end
        else architect=amend
            Architect->>Consumer: step.completed(verdict=amend, remediation)
            Consumer->>Feedback: re-dispatch with remediation
        else architect=supersede
            Architect->>Consumer: step.completed(verdict=supersede)
            Consumer->>Plan: dispatch wf-doc-amend (new ADR)
        else architect=uncertain
            Note over Consumer: cap at 5 attempts (ADR-0032)
            Consumer->>Architect: re-dispatch
        end
    end
```

## Conformance notes

- The architect's three reversal verdicts (`accept-as-is`, `amend`, `supersede`) each route to a different downstream workflow. `uncertain` re-enters arbitration up to a per-task cap (ADR-0032).
- The `validate.override` and `review.override` event pair is the *only* mechanism by which the architect's authority crosses from internal state to GitHub-side mergeability. The bridge is event-projection-driven, not direct API.
- The flush requirement between override INSERT and mergeability VIEW SELECT is implementation-level invariant. Violating it produces the silent-stall failure mode observed on PRs #132 and #133 (2026-05-17).
