---
name: plan
description: Create or update a Plan document for an upcoming changeset, spike, or epic. Use whenever non-trivial work is about to start and we need to record the goal, success criteria, scope (including what's out), sequence, and risks before authoring code or tasks. Plans are the bridge between ADRs (decisions) and work (commits, tasks, PRs). Plans are mutable while in flight and gain a post-mortem when they complete.
---

# /plan — Plan a changeset, spike, or epic

A plan is the operational artifact that turns decisions (ADRs) into work. It is short, skimmable, and concrete. Where ADRs are immutable records of moments, plans evolve while work is in flight and end with a post-mortem.

## When to invoke

- Multi-step work is about to start and the sequence matters.
- A spike, refactor, or feature with non-obvious scope is being committed to.
- Decisions have been made (one or more ADRs) and we need to operationalize them.
- Multiple sessions or contributors will work on this and need a shared reference.

## When NOT to invoke

- Single-task changes that fit in one PR — describe them in the PR body.
- Pure exploration with no commitment — talk it out, don't bureaucratize it.
- Decisions that haven't yet been made — those belong in an ADR via `/decide`.

## File format

Plans live at `docs/plans/<date>-<slug>.md` where `<date>` is the authoring date (`YYYY-MM-DD`) and `<slug>` is a kebab-case short title under ~50 characters.

Plans are not numbered. They are scoped by date and slug. Multiple plans on the same day are fine — date plus slug should be unique.

### Optional frontmatter

A plan may carry a leading YAML frontmatter block delimited by `---`. Fields are optional; omit the whole block when there's nothing to set. Currently supported:

- `auto_merge: bool` — opt out of the ADR-0031 auto-merge cooling-off trigger by setting `auto_merge: false`. Default (omitted / unset / `true`) is **enabled**: PRs from this plan's tasks become eligible for auto-merge after CI is green and the cooling-off window elapses. Set `false` when a plan wants a human to merge each PR manually — e.g. high-blast-radius migrations, plans touching shared schemas, or plans where review nuance matters more than throughput.

Example:

```markdown
---
auto_merge: false
---

# Plan: Title
...
```

## Template

```markdown
# Plan: Title

- **Status:** drafting | active | completed | abandoned | superseded by <date>-<slug>
- **Date:** YYYY-MM-DD
- **Related ADRs:** ADR-MMMM, ADR-OOOO (omit if not applicable)
- **Supersedes:** <date>-<slug> (omit if not applicable)

## Goal

What we are trying to accomplish, in plain language. One short paragraph.

## Success criteria

Concrete, testable outcomes. A reader should be able to look at the system at the end and judge each criterion as met or not met. Avoid "it works" — name the observable behavior.

## Constraints / scope

### In scope
What we will do.

### Out of scope
What we will not do, even if tempted. This list is required, not optional — naming what we will not do is half of scope discipline.

### Budget
Time, headcount, or token budget. If the budget is exhausted before success criteria are met, we abort and write a post-mortem rather than escalate quietly.

## Sequence of work

A short, ordered list of work units. Each unit is sized to roughly one day of focused work. If a unit feels bigger, split it. Mark dependencies inline.

## Diagram (if applicable)

A Mermaid diagram showing the intended end-state. Skip when the plan is purely organizational. When the diagram already exists in a related ADR, reference the ADR rather than duplicating it.

### Diagram type by decision class

| Plan class | Diagram kind |
|---|---|
| Workflow with actor handoffs over time | `sequenceDiagram` |
| Static topology / dependencies / component layout | `flowchart` |
| Lifecycle / state transitions of an artifact | `stateDiagram-v2` |

### Conformance checklist (per ADR-0004)

A conformant diagram uses named actors only, stays at the intent layer, and labels every interaction. Specifically:

- **Named actors only** — every participant is named explicitly; no anonymous participants. Even "the operator" or "the worker" is named.
- **Labels every interaction** with the operation, event, or message name — not just a verb. `pr_merged` beats "merges."
- **Stays at the intent layer** — *what* and *between whom*, not function signatures or class names.
- **Uses the right Mermaid kind** for the plan class, per the table above.
- **Distinguishes synchronous from asynchronous** when it matters (`->>` solid; `-->>` dashed for async/event).
- **Names alternative branches** with `alt`/`else` blocks.

A non-conformant diagram is a defect; reviewers reject plans whose diagrams are vague or decorative.

## Risks / unknowns

What could derail this. What we'll learn during execution that might invalidate the plan. Pair each risk with a mitigation or a "we'll abort if" trigger.

## Decisions captured during execution

A running list, populated as we work. When a real decision emerges (one that future readers need to understand), link to the ADR authored via `/decide`. This section starts empty and is appended to in place.

## Post-mortem

Filled in when the plan transitions to `completed` or `abandoned`.

- **What worked.** Briefly.
- **What surprised us.** Honestly.
- **What should become an ADR, learning, or rule.** Pointers to follow-up artifacts.
- **What this plan teaches us about future plans.** Process feedback.
```

## Authoring conventions

- **Voice is collective first-person plural.** Use "we" throughout. Same convention as ADRs.
- **Default to under 500 words.** Plans should be skimmable. If a plan needs more, the scope is probably too big — split it.
- **Reference ADRs; do not restate them.** If a plan needs to explain *why* the work is happening, link to the ADR.
- **Success criteria must be observable.** "The adapter handles autoscaling" is unmeasurable; "publishing 3 messages results in 3 workers spawning sequentially and 0 workers after the idle window" is measurable.
- **The Out-of-scope list is required.** A plan without explicit non-goals will accrue them silently.
- **Tasks size to ~1 day.** Sequencing reads cleanly; estimate accuracy is acceptable.
- **Scope the docs-currency surface into every code task.** ADR-0030's `docs-current-with-pr` is a *blocking* llm-judge: a PR that adds or changes a code module must also update the touched component's `AGENT.md` (and any cited ADRs/plans). A `sequence_of_work` task whose `scope.files` omits that surface is structurally guaranteed to trip the rule — it bounces to review and then leans on the architect to override, which is both wasteful and a source of false "architect is too permissive" signal. So for every task that creates or modifies code: (a) have the `intent` instruct a "Key surfaces" (or Pitfalls/Navigation) update in the component `AGENT.md` where the change warrants one, plus an `agent-changes/YYYY-MM-DD-<task-short-id-or-pr>-<slug>.md` fragment per `docs/agent-md-schema.md` — never an in-file "Recent changes" entry (the prepend form is the merge-conflict factory the fragment convention eliminated, 2026-06-12); and (b) in `scope.files`, name the fragment path — list the shared `AGENT.md` itself only when prose sections actually change, so concurrently-authored tasks stop being routed through the one shared file when only changelog currency is needed. Author tasks that *pass* the gates; never rely on downstream roles to backfill the docs the gate requires.
- **Scope in the existing tests of any code you modify.** When a task changes a function or module that already has tests, those tests run against the new code path — and loose mocks will trip on a newly-added call (e.g. wiring a DB lookup into a function whose tests use a bare mock session makes the new query read a truthy mock and mis-fire). A task that scopes only its *new* test file leaves the existing suite red, and `wf-ci-fix` can't reach a file outside scope. So when modifying tested code: (a) list the existing test file(s) for that code in `scope.files`, and (b) have the `intent` say to update them (neutralize/mock the new dependency). Same root cause as docs-currency — the task's scope must include everything the gates hold the change accountable for, not just the new files.
- **Make `deterministic` validation robust, not formatting-brittle.** A `kind: deterministic` script that exact-string-greps a multi-arg call or specific formatting (`grep -q "foo(a=x, b=y)"`) fails on valid-but-differently-formatted code (line-wrapped, reordered args, a trailing comma) — a *false negative* the worker can't satisfy, so it loops author→feedback→architect and may never even open a PR (the architect will say the code "looks correct" yet the gate stays red). Make the **`pytest` you specced the actual gate** — it asserts behavior, not text. Use greps only for coarse presence (`grep -q "foo("`, a function or class name), never an exact call signature or formatting.
- **Every tool the gate invokes must exist in the worker sandbox.** A deterministic gate runs inside the agent container, which is intentionally narrower than dev-local: no docker daemon, no live AWS, no network egress by default, and only the tooling our agent image installs (Python + pip extras + `make` + `cdk` + the project test runner). **Do NOT use in a deterministic gate:** `cdk synth` against a live AWS account, any `aws ...` command that hits a real endpoint, `docker run` / `docker compose`, anything that requires network egress to packages or services, or tooling not in the agent image. These fail with cryptic `Script exited 1` errors the architect can't diagnose and the wf-feedback loop can't repair — symptom is the architect verdicting `amend` 5x while the author has logically-correct code (per the 2026-05-27 RAMJAC wedge incident, learning `2026-05-27-deterministic-gates-must-run-in-worker-sandbox.md`). Prefer pure `pytest` invocations, or shell commands that operate on the repo (`make unit_test`, `make lint`, `grep`, `find`). If a check genuinely requires live infrastructure, scope it as an `llm-judge` or as a separate post-merge soak task, not as the deterministic gate.
- **No emojis, no marketing language.** Plans are operational, not promotional.

## Before submitting to Treadmill

After writing the plan doc and before calling `treadmill plan submit`:

1. **Run `treadmill plan validate`** — catches schema violations, missing `description` fields,
   and `depends_on` syntax errors. Fix all findings before proceeding.

2. **Relay to at least one sibling for review** — relay the plan file as an action request:
   ```bash
   python3 ~/treadmill/tools/cc-channels/cc-relay.py \
     --to treadmill-bert --from treadmill-alan --type action \
     --file <plan_path>
   ```
   Follow up with a context message: what to look for (scope gaps, depends_on, sandbox safety,
   infra gotchas). Hold submission until the sibling clears it.

   **Why this step is load-bearing**: A sibling has independent context about the infrastructure
   and codebase. The authoring session has just spent time with the plan and is likely to miss
   mistakes that are obvious to a fresh reader. On the first real use of this pattern (2026-06-09
   scheduler GCP plan), Bert caught 3 critical errors that would have caused active service
   breakage if dispatched: a wrong IAM assumption, a wrong AR region instruction, and a false
   "no existing workflow" claim. The cost of holding for 5 minutes is trivially lower than
   a worker executing broken instructions.

3. **Address review findings** — for CRITICAL items, fix before submitting. For scope gaps and
   minor items, either fix them or explicitly defer with a note in the Risks section.

4. **Relay a patch summary** back to the reviewer if significant changes were made.

## Status transitions

- `drafting` → `active` when work starts.
- `active` → `completed` when success criteria are met.
- `active` → `abandoned` when we stop work without meeting the criteria. The post-mortem is mandatory.
- `active` → `superseded by <date>-<slug>` when a new plan replaces this one. The replacing plan must reference this one.

While `drafting` or `active`, the plan body is mutable — update freely. After `completed` or `abandoned`, edits are limited to status changes and post-mortem additions; the body of the plan is preserved as the historical record of intent.

## After execution

1. Update the status header.
2. Write the post-mortem honestly. Surprises and failures are more valuable than what worked.
3. Author follow-up artifacts: ADRs for new decisions, learnings for new patterns, rules for new constraints. The plan's post-mortem links to them.
4. Tell the user what's done and what artifacts the plan produced.
