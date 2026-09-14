# ADR-0111: The two-pass review is panel breadth + a cross-model verifier

- **Status:** accepted (2026-09-13; operator-directed framing; cross-model panel + sibling co-sign by Ernie, who endorsed the two-axes model)
- **Amends:** ADR-0105 (cross-model review uses siblings and requires two passes)
- **Date:** 2026-09-13
- **Related:** ADR-0107 (model gateway + review panel), ADR-0108 (panel-backed evaluator)

## Context

ADR-0105 required two independent cross-model passes plus a same-family sibling
review, when cross-model meant relaying to per-sibling bridges. ADR-0107 replaced that
with the review PANEL — cross-family reviewers in one command. The operator then asked
(2026-09-13): given the panel, is a same-family sibling review still needed, since the
author is itself a Claude model? The honest answer reframes the two passes.

## Decision

The two-pass review is two DIFFERENT axes, not two opinions:

1. **The cross-model PANEL — breadth.** Many families, but each leg is a single-shot,
   NO-TOOLS, artifact-only call (sandboxed by design, ADR-0108). It reads the text and
   reasons; it cannot run code, grep the tree, or check wiring.
2. **A context-rich VERIFIER sibling — depth.** A full agentic session that runs the
   foil, checks the actual repo/wiring, and ground-truths the claims the panel took on
   trust. The verifier is **preferably CROSS-MODEL** — a Codex sibling (Fran, or a
   future Codex sibling) — so it shares NEITHER the author's family blind spots NOR the
   panel's context-blindness. A same-family (Claude) sibling is the FALLBACK only when
   no cross-model sibling is free.

**Scope the verifier by stakes:** REQUIRED for code changes and high-stakes /
wiring-dependent artifacts (claims that must be ground-truthed); OPTIONAL (panel alone)
for low-stakes, pure-prose artifacts with no external claims.

Evidence, this session: the panel caught ADR-0110's false "agents can merge a non-main
branch" premise by pure logic; the sibling caught the `--author-family` fail-open (RAN
the CLI), the unwired escalation (saw the coordinator template was unchanged), and the
post-merge-observation gap (repo context). Complementary — each caught the other's
blind spot.

## Alternatives considered

- **Incumbent: ADR-0105's "two cross-model passes + a same-family sibling."** Why
  insufficient: the panel now IS the cross-model breadth in one command, and it has a
  Claude leg — so the same-family sibling framed as "another opinion" is largely
  redundant; its real, un-named value is VERIFICATION (tools + repo context).
- **Drop the sibling entirely (panel only).** Rejected: the panel is single-shot +
  no-tools by security design, so it cannot run a foil, grep the tree, or notice an
  unchanged file — the exact class the verifier catches. Dropping it removes the only
  ground-truthing layer.
- **Keep a same-family sibling as the DEFAULT verifier.** Rejected as the default: it
  shares the author's blind spots; prefer a cross-model verifier where the roster
  allows (this is part of why a balanced Codex sibling roster is worth building).

## Consequences

### Good
- Clearer and cheaper: the panel does breadth, the verifier does depth, and low-stakes
  prose can skip the sibling.
- The cross-model-preferred verifier closes the same-family-blind-spot gap the panel's
  Claude leg cannot.

### Bad / trade-offs
- A cross-model verifier is not always available (currently only Fran; Gerald is down),
  so high-stakes Claude-authored work may fall back to a Claude verifier until the
  cross-model sibling roster grows.
- Stakes-gating needs judgment (what counts as "high-stakes / has external claims").

### Risks
- **Falsifier:** a code change or a high-stakes/wiring-dependent artifact is treated as
  reviewed on the PANEL ALONE, with no context-rich verifier having run foils / checked
  wiring; OR a cross-model sibling was available but a same-family one verified a
  high-stakes Claude-authored change with no note of why.

## References

- ADR-0105 (amended), ADR-0107 (panel), ADR-0108 (panel-backed evaluator).
- The /plan, /decide, and adversarial-review skills carry the operational form.
