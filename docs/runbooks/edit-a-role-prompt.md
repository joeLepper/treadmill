# Runbook — edit a role prompt

**Audience:** Treadmill operator who wants to change a role's
`system_prompt` against a running deployment.

**Scope:** Role prompts only. Workflow shape (step list, role refs)
stays code-driven; see [[Q28.e in ADR-0028]] for the rationale.

---

## TL;DR

```bash
treadmill role show role-reviewer > /tmp/prompt.md     # 1. export
$EDITOR /tmp/prompt.md                                  # 2. edit
treadmill role update role-reviewer \                   # 3. PATCH
  --prompt-from-file /tmp/prompt.md \
  --notes "tightened verdict criteria" \
  --pr-url https://github.com/joeLepper/treadmill/pull/42
treadmill role show role-reviewer                       # 4. verify
# (5. watch the next wf-review run hit the new prompt)
```

---

## The mental model

Per ADR-0028, **the DB is authoritative for role prompts after
bootstrap.** `services/api/treadmill_api/starters.py` is a
**bootstrap fixture**, not a runtime spec — editing it has zero
effect on running deployments.

If you change a prompt in `starters.py` and re-deploy, **the
running prompt does not change.** The seed-starters command's 409
behavior (default) preserves operator edits across re-seeds. The
recovery path below is the only way code edits flow to the DB
after the first seed.

This inverts the bunkhouse failure mode — "I edited the code,
forgot to seed, debugged for 30 minutes wondering why nothing
changed." With ADR-0028, the right move is to edit the DB
directly via the CLI.

---

## The supported edit workflow

### 1. Export the current prompt

```bash
treadmill role show role-reviewer > /tmp/role-reviewer.md
```

This prints the live prompt plus a header line with model + kind +
`updated_at`. Strip the header line if you want a clean prompt
file:

```bash
# The header is a single line; tail -n +3 skips header + blank line
treadmill role show role-reviewer | tail -n +3 > /tmp/role-reviewer.md
```

### 2. Edit the file

Use any editor. The file is just plain text — no special syntax.

### 3. PATCH via the CLI

```bash
treadmill role update role-reviewer \
  --prompt-from-file /tmp/role-reviewer.md \
  --notes "reduce false-positive request_changes verdicts" \
  --pr-url https://github.com/joeLepper/treadmill/pull/42
```

Both `--notes` and `--pr-url` are optional but recommended for
high-stakes edits — they're written to the `role_versions` audit
trail so a future reader can answer "why did this prompt change?"

The command prints the new version number on success.

### 4. Verify the edit landed

```bash
treadmill role show role-reviewer
```

The live prompt should now be your edit. Sanity-check the header
line's `updated_at` is recent.

For the full audit trail:

```bash
treadmill role versions role-reviewer
```

Renders a table with version, created_at, created_by, notes, and
pr_url for every edit.

### 5. Watch the next workflow run

The next `wf-review` (or whichever workflow uses this role) reads
the new prompt directly from the DB — no restart, no re-seed
required. Tail the worker logs:

```bash
treadmill-local logs worker --follow
```

Or watch the next PR's review for behavioral evidence of the new
prompt.

---

## Recovery: when the DB diverges from what you expect

If a bad edit went in, or operator-edits got out of sync with
what `starters.py` says, **the recovery path is to reset prompts
from code:**

```bash
treadmill workflows seed-starters --reset-prompts-from-code
```

This prompts for confirmation (destructive of operator edits) and
then PATCHes every role's `system_prompt` back to the code-side
definition in `starters.py`. Pass `--yes` to skip the confirmation
for scripted recovery:

```bash
treadmill workflows seed-starters --reset-prompts-from-code --yes
```

**What this does NOT do:** does not touch workflow shape, does not
rollback to a specific prior version, does not delete the audit
trail. The reset appends a new `role_versions` row recording the
reset for every role it touches.

**When to use:** the operator has lost confidence that the live
prompts match `starters.py` and wants the bootstrap shape back.
For "I want to revert to the prior version," there's no v1 CLI
affordance yet (see Q28.b — `role rollback` deferred). Workaround:
`treadmill role show <id> --version <n>` to dump the prior content
to a file, then `role update --prompt-from-file` against that file.

---

## Why this design

* **Code edits to `starters.py` are inert against running
  deployments.** This is by design — the bunkhouse failure mode
  was "I edited code, forgot to seed, behavior didn't change,
  spent 30 minutes debugging." Inverting it: the DB is the source
  of truth, and code only seeds a fresh install.

* **Every prompt edit appends an audit row, not overwrites
  history.** The `role_versions` table grows by one row per
  `role update` call (or per `--reset-prompts-from-code` row).
  The `notes` + `pr_url` columns let high-stakes edits link back
  to their rationale.

* **Workflow shape is intentionally not editable via the CLI**
  (Q28.e). Shape changes (step list, role refs) are higher-stakes
  and deserve an ADR + code review — `gh pr` is the right forcing
  function, not a CLI flag.

See ADR-0028 §"Decision" + the resolved Open Qs for the full
rationale.

---

## Note: ADR-0029 and role-validator

Per ADR-0029 (the Ralph-loop validation architecture), the
`role-validator` becomes a structural artifact: its `system_prompt`
exists only to satisfy the workflow→role schema and is never invoked
at runtime. The `wf-validate` worker dispatches validation tasks
directly to subprocess (for deterministic checks) and separate Claude
Code calls (per llm-judge entry), bypassing the role entirely.

Editing `role-validator`'s prompt via `treadmill role update` remains
possible but has no effect on validation behavior. If you're
implementing validator customization, consult ADR-0029 for the
subprocess/llm-judge dispatch mechanism.

---

## Related

* [ADR-0028 — DB-authoritative workflow/role configs](../adrs/0028-db-authoritative-workflow-configs.md)
* [ADR-0029 — Ralph-loop validation runner + rule engine](../adrs/0029-ralph-loop-validation-runner-and-rule-engine.md)
* [In-session sequencing plan](../plans/2026-05-13-in-session-sequencing.md)
* `treadmill role --help` for the CLI surface
