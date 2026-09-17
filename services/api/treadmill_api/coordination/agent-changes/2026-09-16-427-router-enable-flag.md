# 2026-09-16 — ADR-0118/0119: ROUTER_DISPATCH_ENABLED is a real Settings field + wired for cutover

- `config.py`: `router_dispatch_enabled: bool` (alias `TREADMILL_ROUTER_DISPATCH_ENABLED`, default
  False). Previously read via `getattr(settings, ...)` — now a first-class Settings field so the
  flag actually turns the router on. When true the lifespan starts the dispatch consumer +
  reconcile/integration sweeps + worker sink; SC6-scoped (acts only on `substrate=='router'`
  plans; legacy plans stay on the agent coordinator).
- `tools/local-adapter/runtime.py`: the dev-local api container env sets
  `TREADMILL_ROUTER_DISPATCH_ENABLED` from `cfg.get("router_dispatch_enabled", True)` — the
  ADR-0118/0119 cutover enable for dev-local (overridable via deployment cfg).
