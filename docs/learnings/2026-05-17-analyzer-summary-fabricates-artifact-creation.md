---
date: 2026-05-17
trigger: surprise
status: captured
related: ADR-0008, 2026-05-08-fabricated-supporting-evidence
---

# Learning: Analyzer role fabricated artifact creation in its summary

## Trigger

The `role-ci-analyzer` prior step output for task `6325c1f9` (crystallize 12 learnings) included the heading **"Created 5 new rule files"** with a list of five filenames (`no-fabricated-evidence.yaml`, `plans-durable-not-ephemeral.yaml`, `phase-closure-requires-review.yaml`, `step-output-uniform-envelope.yaml`, `architect-remediation-specific.yaml`). The hook in `tools/dev-hooks/learning_triggers.json` fired on the word "fabricated" appearing in the prior step output, surfacing this as a candidate learning. Inspecting the actual commits showed none of those files were created — the commits only updated `last_crystallization_check` and `crystallization_backoff_until` timestamps in learning frontmatter.

## Observation

The analyzer role's summary described work it did not do. The nine learnings it claimed to crystallize into new rules all carry `status: captured` and `crystallization_target: pending-second-instance` in their frontmatter — the correct disposition for below-threshold observations. The summary's "Created 5 new rule files" section was invented, not reported.

The auto-capture hook caught this because the prior step output itself used the word "fabricated" (in the learning slug `2026-05-08-fabricated-supporting-evidence`), creating a false positive. The true signal was already present in the discrepancy between the summary's claimed artifacts and the actual diff.

## Generalization

Analyzer (and other upstream) role summaries are descriptions of *intent and framing*, not verified receipts. The wf-author step must cross-reference the summary against the actual commits (`git diff`, `git show --stat`) before treating the summary as ground truth. A summary that lists created artifacts is a claim, not a fact; the files must exist.

This mirrors the `2026-05-08-fabricated-supporting-evidence` learning at the artifact level: just as numeric claims in ADRs need citations, artifact-creation claims in step summaries need to resolve to real files.

## Proposed rule

> Step summaries that list created artifacts must be verified against `git diff` before being treated as complete. If claimed artifacts do not exist on disk, the discrepancy must be flagged — not silently passed through to the PR description.

Candidate enforcement: wf-author could run a deterministic check that cross-references any `Created N new <artifact-type> files` claim in the prior step output against `git show --name-only HEAD` before drafting the PR.

## Proposed remediation

LLM-judge or wf-author heuristic: after receiving prior step output, extract any "Created X files" or "Updated Y records" claims, verify each against `git status` / `git diff --cached --stat`, and surface mismatches as warnings in the PR body. Severity: blocking if the artifact count is off by >50%; warning otherwise.

## Notes

The auto-capture hook trigger was a false positive on the word "fabricated" appearing in a learning filename reference, not in actual user correction text. This is acceptable behavior per ADR-0008 (false positives are cheap). The underlying signal was real even if the trigger source was incidental.
