---
name: rule
description: Formalize a learning (or pattern) into an enforceable rule. Use when a learning has been observed enough to deserve enforcement across all Treadmill-managed projects. Rules are cross-project policy with attached remediations. They live in docs/knowledge-base/rules/ and follow the schema established in ADR-0006. Different from /learning (raw observation) and /decide (architectural decision record).
---

# /rule — Formalize a learning into an enforceable rule

A rule is a YAML document that says: *here is a constraint, here is how we check whether it is met, and here is what happens when it is not*. Rules are the durable, enforceable form of patterns we have learned. They apply across every Treadmill-managed project unless explicitly scoped narrower.

## When to invoke

- A learning has been corroborated — by a second incident, by deliberate review, or by clear evidence that the pattern is general — and we want to enforce it rather than rely on memory.
- A pattern is so foundational that we want it enforced from a project's first commit, even before the supporting learning has been observed in that project.
- A reviewer or human notes that "we keep doing this wrong"; the appropriate response is a rule, not another reminder.

## When NOT to invoke

- A single learning, fresh, with no second incident — the bar is "earned the right to be enforced," and one observation rarely meets it. Capture, watch, then crystallize.
- A Treadmill-internal architectural decision (use `/decide`).
- A one-off remediation that doesn't generalize (just fix it).
- An aspirational ideal with no defined check ("be a good engineer"). Rules must be falsifiable.

## File format

Rules live at `docs/knowledge-base/rules/<slug>.yaml` where `<slug>` is a kebab-case noun phrase identifying the rule. The slug must match the file name (without extension) and must match the rule's `name:` field. Slugs are stable and human-readable — they are how rules are cited.

## Schema

Per ADR-0006:

```yaml
name: <slug>                         # matches filename without .yaml
description: <one-line summary>      # what the rule asserts
status: active                       # active | deprecated | superseded-by-<slug>
created: YYYY-MM-DD
crystallized_from:                   # at least one entry; required
  - <learning-slug-or-other-evidence>
applies_to:                          # optional; defaults to all managed projects
  - <project-name-or-glob>
checks:                              # at least one
  - id: <kebab-case-id>
    type: deterministic              # or: llm-judge
    description: <what this check evaluates>
    # for deterministic:
    script: <repo-relative-path-to-script>      # exit 0 = pass, non-zero = fail
    # for llm-judge:
    prompt: |
      <prompt template; receives context as input>
    severity: blocking               # or: warning | advisory
remediations:                        # at least one
  - on: <check-id>:fail              # or: <check-id>:uncertain
    action: block-merge              # or: warn | comment-on-pr | open-task | notify-human
    target: pr-author                # or: repo-owner | treadmill-orchestrator | other
    message: <optional template>
references:                          # optional but encouraged
  - <ADR-NNNN, KB-ADR-NNNN, learning-slug, or external link>
```

## Authoring conventions

- **Voice is collective first-person plural** in the `description`, `message`, and `references` fields when prose. Same convention as ADRs and learnings.
- **Every rule must reference at least one source learning** in `crystallized_from`. A rule with no provenance is a fiat — they decay.
- **Every rule must have at least one check and at least one remediation.** A rule that does not enforce is a wish.
- **Prefer deterministic checks where the rule is mechanical**, and add an LLM judge to catch what the script cannot see. Hybrid is the default; pure-LLM should be rare.
- **Severity declares default action**, but explicit `action:` on a remediation wins. Be conservative — `block-merge` is for invariants, not preferences.
- **Every script path must be real and runnable.** Place scripts under `tools/rule-checks/<rule-slug>/<check-id>.sh` so they version with the rule.
- **Every LLM-judge prompt must include**:
  1. A clear definition of pass / fail / uncertain output.
  2. Required output JSON shape (so the engine can parse).
  3. The context inputs the prompt expects (diff, plan, file paths, etc.).
- **Status starts at `active`.** Move to `deprecated` when the rule no longer reflects current opinion; move to `superseded-by-<slug>` when a new rule replaces this one. Never delete rules — they are the durable record.

## Status transitions

- `active` → `deprecated` (the rule no longer applies; we keep the file as historical record).
- `active` → `superseded-by-<slug>` (a new rule replaces this one; the new rule's `crystallized_from` should reference this one).
- `deprecated` → `active` (revival is possible; rare).

## After authoring

1. Confirm the file is at `docs/knowledge-base/rules/<slug>.yaml`, the slug matches the `name:` field, and the script paths in `checks:` are real.
2. Update each source learning's `status` to `crystallized-into-rule-<slug>`.
3. Tell the human the rule's slug, its severity, and which remediation actions it dispatches.
4. **Do not implement the engine here.** Authoring a rule is documentation. Wiring it to evaluate is a separate task — see ADR-0006's deferred engine ADR.

## Crystallization vs. rule

A learning is the raw observation; a rule is the formalized constraint. The relationship:

- One learning may seed one rule (a clear pattern with a clear remediation).
- One rule may aggregate several learnings (a pattern observed in multiple forms).
- Many learnings may never become rules — they remain evidence that informs but does not enforce.

Authoring a rule from a single fresh learning is usually too soon. Authoring a rule from a pattern that has been independently observed in multiple sessions is usually right.
