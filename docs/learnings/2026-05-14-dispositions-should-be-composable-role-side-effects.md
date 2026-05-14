---
name: dispositions-should-be-composable-role-side-effects
date: 2026-05-14
status: open
trigger: Operator framing 2026-05-14, after the code-disposition
  `gh pr create` already-exists bug surfaced: *"Remember that we want
  that [shared code] to be re-used because we want users to be able
  to update roles in the future. So it needs to be resilient. In fact,
  some future roles might not create or act on PRs at all."*
session_id: 2ccc6390-0915-4884-8fc8-86692b71895d
captured_via: operator-direct
---

## The lesson

The per-kind disposition handlers (`runner_dispositions/{code,review,
analysis,plan_doc,validation}.py`) need to compose role-specific
side effects, not bake assumptions about a particular workflow's
shape into the shared code path.

The PR-creation bug surfaced this concretely:

* `code.py:handle` shared by wf-author + wf-feedback + wf-ci-fix +
  wf-conflict. wf-author needs to **create** a new PR after pushing
  commits. The re-author workflows (wf-feedback, wf-ci-fix,
  wf-conflict) push to an **existing** PR's branch and trigger
  `pr_synchronize`; `gh pr create` is wrong for them.
* My first reaction was "branch on workflow_id and skip
  `gh pr create` on the re-author workflows." Joe's pushback: that's
  brittle. The disposition handler should be **resilient** —
  composable across workflows that exist today + workflows that
  don't exist yet.

The deeper framing: **Treadmill is role-agnostic and side-effect-
agnostic.** ADR-0022's `output_kind` taxonomy currently covers
code / review / analysis / plan_doc / (soon) validation. Future
kinds we haven't named yet — a role that publishes a doc to a CMS,
a role that calls an external API, a role that updates a config in
a remote system, a role that posts to Slack — would each have
entirely different post-Claude side effects. The disposition layer
needs to be ready for that variety.

## How to apply

When designing or modifying a disposition handler:

1. **Make every side effect idempotent.** "Open PR for this branch"
   should detect an existing PR + return its number rather than
   raise. "Post comment" should be safe to call again. "Push branch"
   already is. Idempotency is the floor.

2. **Pull side effects out of the shared handler when they don't
   universally apply.** PR creation isn't universal to all
   code-emitting workflows; it shouldn't live in the bottom of
   `code.py`. Better shape: `code.py` produces a `StepOutput`
   carrying the new commit SHA + branch + maybe-pr-number; a
   separate post-disposition action layer decides what to do with
   that envelope based on the workflow + role config.

3. **Resist `if workflow_id == 'wf-X'` branching in the
   disposition.** Workflows are open; new ones will be added. The
   disposition shouldn't know about workflow names. The
   per-workflow shape lives in the workflow's step list + the
   role's configuration, not in the handler's `if` ladder.

4. **Expect future roles to have side effects we haven't named
   yet.** When you find yourself making the disposition handler
   smarter about "the PR" or "the branch" or "the commit," ask: is
   this assumption universal across roles, or am I conflating
   wf-author's particular shape with the framework?

## Connection to ADR-0029

ADR-0029 already establishes the project-agnosticism principle for
**checks**: Treadmill ships no hardcoded `pytest` / `uv` / `cdk`
checks; everything comes from operator-authored rules + per-task
validation blocks. This learning extends the same principle to
**dispositions**: Treadmill ships no hardcoded "wf-author creates
PR, wf-feedback updates existing PR" assumption. The disposition
layer should be flat enough that adding a new kind doesn't require
editing the existing kinds' handlers.

Future ADR (post-ADR-0029) likely names the disposition surface
explicitly: what's the schema of a `StepOutput` for an arbitrary
role-kind? What's the post-disposition action menu? This learning
is the motivating context.

## Related

- [[dual-ingress-paths-need-a-shared-facade]] (yesterday's learning)
  — same class: shared code paths drift when assumptions baked in
  one place don't apply to the parallel call site.
- [[validation-targets-task-intent-not-generic-correctness]] (this
  session) — extends the agnosticism principle from "rules don't
  ape CI" to "dispositions don't ape any specific role's shape."
- ADR-0022 (output_kind dispatch) — the existing taxonomy this
  learning extends.
- Task #120 (code disposition: skip gh pr create on re-author
  workflows) — the proximate bug; this learning reshapes its
  intended fix.

## Open items

- Should the PR-creation step move OUT of `code.py` and into a
  separate post-disposition action layer? Probably yes. Sketch a
  schema (post-disposition action = workflow_id + role_id +
  StepOutput → side effect) once ADR-0029 lands and we revisit the
  disposition architecture.
- Document this as a constraint on future role authors: dispositions
  must be idempotent + must not assume any particular external
  artifact (PR, comment, file) is universal.
