---
date: 2026-09-12
trigger: surprise
status: captured
related: ADR-0107
---

# Learning: A per-key model allowlist does not cover router-configured fallbacks

## Trigger
The ADR-0107 DB-policy spike recorded an HTTP 403 when an open-weight-only virtual
key requested `claude-sonnet-4-5`, and marked the allowlist-enforcement unknown as
resolved. Gerald (open-weight sibling) re-verified against litellm 1.100.1 and found
the proof covers only one of two code paths.

## Observation
The spike issued a DIRECT request for an out-of-allowlist model. LiteLLM's per-key
allowlist fires on that path. It does NOT fire on a fallback the router chooses,
because the router picks a `router_settings.fallbacks` target AFTER auth. LiteLLM
1.100.1 ships a native guard for this: `general_settings.enforce_fallback_model_access`
(`fallback_model_access.py:28`), which DEFAULTS OFF. When off, an open-weight-only key
can reach `claude-sonnet-4-5` through a configured fallback entry. The guard is plumbed
into the router (`proxy_server.py:5749`, `:6209`), invoked per target
(`fallback_event_handlers.py:277-293`, `:400`), fails closed on lookup error
(`fallback_model_access.py:39-44`), and covers `fallbacks`, `context_window_fallbacks`,
and `content_policy_fallbacks`. Separately, the alias→provider-identity dimension has NO
runtime guard: `can_key_call_resolved_model` checks by model NAME only
(`auth_checks.py:4282-4300`), so an alias pointed at an arbitrary `api_base` still passes.

## Generalization
An enforcement proof is scoped to the exact code path the foil exercised. "Direct call
is denied" does not prove "every route to that model is denied". A gateway has more than
one route to a model (direct, fallback, alias); each route needs its own foil. We tend to
generalize a single green to the whole surface.

## Proposed rule
Prove a deny on every route to a forbidden resource, not one. For a model gateway:
foil the direct call, the fallback-routed call, and the alias-routed call separately.

## Proposed remediation
Gateway migration must: (1) SET `general_settings.enforce_fallback_model_access: true`
and re-spike a fallback-routed out-of-allowlist request (expect the primary model's error
returned, target skipped); (2) keep the config-side guard for alias→provider identity
(api_base allowlist + provider-tag validation) — no runtime guard exists for that half;
(3) confirm, not assume, that the guard covers the sync router path — `run_async_fallback`
is the async path (`router.py:682`); `function_with_fallbacks` (`router.py:2113`) does not
reach it. Proxy HTTP entrypoints are async, so this is likely moot, but a sync in-process
caller would bypass the guard. Fold all three into the gateway build plan.

## Notes
Code citations supplied by Gerald from `~/gerald/shim/venv` litellm 1.100.1 — the version
the spike ran. Splits ADR-0107 line 231's "fallback/alias closure over router_settings"
into two dimensions with now-different answers: fallback = native opt-in flag; alias =
config-side, still unguarded.
