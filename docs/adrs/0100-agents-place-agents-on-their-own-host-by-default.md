# ADR-0100: Agents place agents on their own host by default; placement is directed, never scheduled

- **Status:** accepted (2026-08-13, operator)
- **Date:** 2026-08-13
- **Related:** ADR-0095 (named agents bind to hosts), ADR-0094 (hosts survive isolation; nothing adopts their agents), ADR-0092 (pool-allocated subscription scheduler — proposed, orthogonal), ADR-0087 (long-lived team execution model)

## Context

ADR-0095 gave us the host **binding** — a label is bound to a host, and a per-host guard refuses, fail-closed, to start a label that is not bound to that host. It deliberately did not decide two things: **who writes a label→host binding**, and **whether placement is decided centrally**. Until those are settled, "an agent runs on a host" has a mechanism but no policy.

The operator asked whether a PM agent (e.g. Potter) could "choose where to start agents." The tempting answer is a scheduler that allocates agents across a pool of hosts. We reject that shape on evidence. ADR-0094 records what a central placement authority did to `exec_otp`: its reconciler adopted 13 agents onto one cloud node in 28 seconds after a brief link drop, the node ran at its ceiling for three and a half hours, and the OOM killer ended it — a sub-minute network blip became a 3h38m outage. A scheduler that decides and moves placement reintroduces exactly the load-concentration and indispensable-central-state that ADR-0094 removed. ADR-0092 (subscription pooling) is a different axis — which Claude *subscription* a team leases, not which *host* an agent runs on — and remains proposed; it is not the answer here.

The operator's directive is direct: **"We don't want a scheduler. An agent should default to starting agents on their host unless directed to do otherwise."**

## Decision

We decided that **an agent that starts another agent places it on the starting agent's own host by default, and may direct a different registered host explicitly. No scheduler, allocator, or reconciler decides or changes placement.**

Concretely:

1. **Default is host-local.** When an agent starts another agent, the new agent's label→host binding (ADR-0095) is written to the *starter's* host (`TREADMILL_HOST`). Co-location is the default because it needs no decision and no network.
2. **Directed override.** The starting agent may name a different host that is present in the host registry; that host becomes the binding. This is how a PM chooses placement — by directing it at start time, not by asking a scheduler.
3. **No central placement authority.** Nothing allocates agents across a pool, and — per ADR-0094 — nothing relocates an agent because its host is unreachable. Placement is set once, at start, by the starting agent's intent.
4. **Directed placement fails closed.** If a directed host is unregistered or unreachable, the start is refused and surfaced to the starter; it is never silently rerouted to another host. Automatic rerouting would be scheduling by the back door — the precise thing this ADR forbids.

Placement is therefore an act of the starting agent's intent, expressed once, enforced thereafter by ADR-0095's guard and protected by ADR-0094's no-adoption rule.

## Alternatives considered

- **A central scheduler / pool-allocator that places agents across hosts.** Rejected on ADR-0094's evidence: a central placement authority concentrates load and makes one machine indispensable; `exec_otp` turned a link drop into a multi-hour outage this way. It also erases the operator's and PM's intent about where work should run.
- **Pin every agent's host statically in config.** Rejected: it is a fixed routing fact with no per-start intent, so a PM cannot direct a one-off placement, and it does not generalize as agents come and go.
- **Reuse ADR-0092's pool allocator for hosts.** Rejected: 0092 leases *subscriptions*, not *hosts* — a different axis — and it is itself a scheduler, which is what we are declining.

## Consequences

### Good
- Nothing to build or operate as a scheduler; placement is a field written at start.
- Co-location is the cheap, network-free default.
- A PM (Potter) gets real control — directed placement — without a central authority.
- Composes cleanly with ADR-0095 (binding + fail-closed guard) and ADR-0094 (survive isolation, no adoption): once placed, an agent is never moved involuntarily.

### Bad / trade-offs
- No automatic load balancing. Concentration is possible if a starter directs many agents at one host; this is the operator's/PM's responsibility, not the system's, and is visible rather than automatic.
- A directed host that is down blocks that start (fail-closed) rather than finding another home — intentional, but it means directed placement needs the target host present.

### Risks
- **A back-door scheduler creeping in.** Any future "helpful" auto-reroute or rebalance is this ADR's failure mode. The falsifier (ADR-0098): no code path relocates or reroutes an agent's placement on the basis of host load or reachability — the same class of check ADR-0095 already commits for adoption. A fail-closed *refusal* of an unreachable directed host (Decision 4) is not a reroute and is not a violation; the falsifier scopes to relocate/reroute, never to refuse.

## References
- ADR-0095 (named agents bind to hosts) — the binding mechanism this policy writes.
- ADR-0094 (hosts survive isolation) — the no-adoption rule this placement policy relies on.
