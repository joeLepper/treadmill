---
date: 2026-06-21
trigger: correction
status: captured
related: ADR-0084
---

# Learning: A third-party tool demanding full-account-access is an operator grant, never an agent's to approve or induce

## Trigger
During the osmo cross-category harvest, the paid Apify Amazon-search actor
(`axesso amazon-search-scraper`) returned `403 full-permission-actor-not-approved`
on run — it demands FULL ACCESS to the operator's Apify account, approvable only by
clicking `approvePermissions=true` in the operator's console. The coordinator
(treadmill-carla) root-caused it and explicitly refused to approve it herself,
escalating to the operator: "granting a 3rd-party scraper full account access is a
security decision + an access-control change = Joe's call, not mine." A prior
`HTTP 200` GET on the actor's metadata had looked fine — only the run-POST hit the
permission wall.

## Observation
An accessible-looking third-party tool (GET 200 on metadata) can still demand a
full-account-access grant at execution time. The coordinator treated that grant as
out of bounds for an agent and escalated rather than clicking it (or inducing the
operator to click it to unblock itself).

## Generalization
Permission/access-control grants on the operator's accounts — especially "give this
third party full access" — are operator security decisions. Agents (workers AND
coordinators) should expect tools to surface such walls, must not approve or
self-induce the grant to unblock their own task, and should prefer a no-grant path
(a less-privileged tool, or a free/owned alternative) before asking the operator to
expand access. When a grant is genuinely needed, escalate with the trade stated
plainly ("full account access just to calibrate a price is a poor trade").

## Proposed rule
An agent never approves, clicks, or induces a third-party full-account-access /
permission grant on the operator's accounts. It escalates the decision to the
operator with the security trade-off explicit, and pursues a no-grant alternative
first.

## Proposed remediation
none (operator-judgment gate; reinforced by the existing prohibited-actions list —
"modifying access controls or sharing permissions"). Pairs with preferring
free/owned data sources over privileged third-party ones where they yield the same
signal.

## Notes
The momentum-density proof did not need the privileged actor at all — the free,
owned Target review APIs deliver the same signal with no grant and no spend. Reaching
for the privileged path to unblock fast would have traded the operator's account
security for speed it didn't need.
