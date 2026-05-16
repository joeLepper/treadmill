---
date: 2026-05-15
trigger: surprise
status: captured
related: ADR-0011, ADR-0012, ADR-0029, ADR-0038
---

# Learning: silent wf-author failures were hidden by task_status view clause 6d

## Trigger

2026-05-15 mid-session investigation: tasks 209f4d8e (schedules table) and
0739cee3 (validator_tuning prompt) reported ``derived_status='done'`` in
the API + heartbeat, but neither produced any commits on main. Direct
grep against the expected files (``schedule.py``,
``starters.py:validator_tuning``) showed both targets were missing.
Investigation surfaced that the role-code-author had written prose
summaries claiming success but the deterministic author-side validation
(per task #121 / ADR-0029) exit-1'd, so no PR was opened. A systematic
audit revealed 13 tasks in the same false-done state.

## Observation

The ``task_status`` view (alembic 0002) reads ``workflow_run_steps.status``
to decide failure (clause 5) and falls through to ``'done'`` (clause 6d)
when the latest run completed and no PR exists. But ADR-0012's
``StepOutput`` envelope carries ``decision`` as a *workflow* outcome
separate from the *step's* execution status: a wf-validate step whose
verdict was fail has ``status='completed'`` (the step ran successfully)
and ``output->>'decision'='fail'`` (the workflow said no).

The view never inspected ``decision``. Clause 5 missed silent failures;
clause 6d painted them as done.

## Generalization

Where we have two adjacent encodings of "success" — one structural
(``status``), one semantic (``decision``) — and only one is used in a
projection, the projection will quietly lie at the boundary cases.
``triggers.py`` (ADR-0038 deadlock arbitration) and the
``task_status`` view *both* read step rows but consult different
fields. They have to agree, or one of them is wrong for some
population.

A view that's the single source of truth for "is this task done"
must read every signal the writers emit. Compatible with the
collaborator's writes is not enough.

## Proposed rule

Any view or projection that summarizes lifecycle state from
``workflow_run_steps`` must consult both ``status`` and
``output->>'decision'``. A status='completed' row with
decision='fail' is a failed workflow, not a finished one.

## Proposed remediation

Deterministic — a CI check that greps each VIEW SQL definition for
references to ``workflow_run_steps.status`` and asserts the same SQL
also references ``output->>'decision'`` (or carries an explicit
exemption comment). Alternative: a pytest that constructs a fixture
with a (status='completed', decision='fail') step row and asserts
the view does not return ``derived_status='done'``.

## Notes

- Fix shipped as alembic migration 0017
  (``0017_task_status_surface_decision_fail.py``). Clause 6d now
  inspects ``output->>'decision'`` before falling through to 'done'.
- Considered patching the consumer instead (write
  ``status='failed'`` when decision='fail') and rejected — would
  break ``triggers.py`` line 643-656 (the ADR-0038 arbitration
  trigger) which explicitly filters on ``status='completed'`` AND
  reads ``decision`` to fire role-architect.
- The view fix is upstream of every consumer that ever read
  ``derived_status``: the heartbeat monitor, the CLI's task list,
  the dashboard. None of them have to change.
- Sibling discovery: this also explains why task #124 ("Reconcile DB
  task state with merged-PR reality") felt under-specified — the
  apparent "branch-name fallback" gap was actually a downstream
  symptom of clause 6d.
