---
date: 2026-05-18
trigger: pattern
status: captured
related: ADR-0033, ADR-0036
---

# Learning: wf-author PR body synthesis leaks session narration into the Summary line

## Trigger

All four PRs open on 2026-05-18 (#136, #137, #138, #143) had Summary lines that pasted the worker's own session-meta narration after the title. Examples:

- **#136** *(CLI plan submissions auto-flip status drafting → active)*: "— Now let me look at what Bash commands appeared in the current conversation context — the transcript patterns I can see directly from this session:"
- **#137** *(Delete `_VERDICT_INNER_RE` regex from review.py)*: "— The git staging and commit operations each need approval. The file changes are complete — here's a summary of what was done:"
- **#138** *(role-crystallization-judge gains prose-verdict fallback parser)*: "— The skill is updating the settings. Let me wait for it to complete before trying git operations again."
- **#143** *(role-rule-corpus-auditor in starters.py)*: "— I was unable to access the transcript directory (`~/.claude/projects/`) — the session security policy restricts file access to the project working directory only. I can only analyze commands observed in the **current session**."

Each Summary's first bullet has the shape: `- <pr_title> — <free-form narration about the worker's own session state>`. The narration content is unrelated to the change; it talks about tooling friction, permission prompts, file-access policies, etc.

## Observation

The wf-author role's PR-body synthesis is reading the worker's *meta-commentary* (introspective remarks the model emits while operating) and threading it into the Summary as if it were a per-bullet detail. The title prefix (`<pr_title> — `) is the synthesizer's template; what comes after should be a one-line "what changed and why" but is actually whatever sentence the model happened to emit at synthesis time.

The pattern repeats across at least four PRs authored by different roles (`role-code-author` for the regex/feature PRs, `role-author` for the role-additions), so it's not a single-role bug — it lives in the shared synthesizer.

ADR-0033 specifies what belongs in PR bodies; ADR-0036 specifies the 5-section structure (Summary, Why, Test plan, Validation, …). The Summary section's contract is "short bullets describing the change," not "stream-of-consciousness from the authoring agent."

## Generalization

When an agent synthesizes a structured artifact (PR body, commit message, release note) from a transcript context, any reasoning-trace text in that context is candidate input. Without an explicit filter ("only include statements about the diff, never about the agent's own process"), the synthesizer will happily quote session-meta into the artifact and ship it.

This is the artifact-synthesis sibling of the "implementation is already in place" failure: the synthesizer is reading a context window that mixes work-product with process-commentary, and the structural template doesn't distinguish them.

The leak is operator-visible (PR readers see it on every PR) and erodes trust in the partnership's output quality. It's also corpus-poisoning: future crystallization runs, future learnings, future training data — all will see these narration leaks as canonical artifact content.

## Proposed rule

The PR body synthesizer must include an explicit anti-narration clause in its prompt: *"Never include statements about the authoring session's tooling, permissions, file access, prompts, or skill behavior. Only include statements about the diff and its rationale."* The synthesis output should be validated against a small forbid-list of phrases (`"let me"`, `"I was unable to"`, `"the skill is"`, `"the current session"`, `"the transcript"`, `"approval"`, etc.) at author-side validation; failures route the PR back to the author for re-synthesis before opening.

## Proposed remediation

- Locate the PR-body synthesis prompt (likely in `services/api/treadmill_api/starters.py` under the wf-author roles or the dedicated PR-body synthesizer role).
- Add an explicit "Forbidden phrasings" subsection to the synthesizer's prompt, listing the narration signatures above and instructing the model that any output containing them must be regenerated.
- Add an author-side validation check (`pr-body-no-session-narration`) that scans the synthesized body for the forbid-list patterns; verdict `fail` on hit.
- Backfill: open a follow-up task to re-synthesize the bodies of #136/#137/#138/#143 after they merge (or leave as-is — they're already merged; the corpus poisoning is bounded).

## Notes

- This is the same shape as 2026-05-16-architect-remediation-must-name-paths-and-verbs.md (prompt sharpening to forbid a specific failure-mode phrase). Both are corrections via prompt anti-pattern forbid-lists.
- Discovered while triaging the four stuck PRs on 2026-05-18 for manual merge after auto-merge was blocked by spurious validator failures — orthogonal to ADR-0042 but observed in the same incident window.
- Verify post-fix: the next wave of wf-author PRs should have clean Summary lines. If the pattern persists, the synthesis prompt isn't where the narration is entering — investigate the upstream context-builder instead.
