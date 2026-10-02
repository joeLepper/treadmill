---
date: 2026-10-02
trigger: correction
status: captured
related: ADR-0008
---

# Learning: A relayed paraphrase is not the operator's verbatim position

## Trigger
Alan (EiC) relayed a task that quoted Joe: "we'll need to be able to derive a token value
for a given AR to really drive that question." I anchored a whole scoping section and a
"decision for Joe" on that phrasing. When I put the decision to Joe, he said: "I don't
remember saying" that, and "I think that you two are asking me to make a decision for a
feature that I haven't been read into."

## Observation
I treated a peer's relayed paraphrase as the operator's exact words and built a
feature-shaped decision around it, then asked the operator to adjudicate that feature. The
operator had not been read into the feature; the framing was sibling-generated, not his.

## Generalization
A directive that reaches us through a peer is second-hand. The peer may compress, infer, or
mis-attribute. When we quote the operator back to the operator, or build a decision on a
quoted phrase, we must mark it as relayed and let the operator confirm the intent — not
present it as their settled position. We also tend to escalate sibling-generated feature
details to the operator as "decisions," when the operator never scoped that feature.

## Proposed rule
Never attribute a quoted phrase to the operator unless it came directly from the operator in
this session; mark relayed directives as relayed and confirm the intent before acting on the
wording. Do not ask the operator to ratify a feature detail they were not read into — bring
the question only after the operator has the context, or resolve it among siblings.

## Proposed remediation
When a task arrives via a peer with an operator quote, restate it as "relayed via <peer>; I
read it as X — confirm?" rather than as the operator's words. Before surfacing a decision to
the operator, check that the operator scoped the feature; if not, give context first, not a
choice. Pairs with [[feedback_dont_ask_joe_to_ratify_sibling_details]] and
[[feedback_relayed_directive_needs_operator_confirm]].

## Notes
Joe's correction also redirected the substance: keep the AR clean of eval-system knowledge
(the client registers the run), and make the API usable by any @netlify.com user rather than
admin-only. Those are captured in the scoping plan, not here.
