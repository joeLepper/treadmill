# treadmill_api/routers

## Purpose

This directory contains the FastAPI routers that expose the Treadmill API
surface. Each file maps to one domain (team configs, tasks, host registry, etc.)
and is registered in `app.py` via `app.include_router(...)`. All routers use
the prefix `/api/v1` and depend on `get_session` from `dependencies_db.py`
for database access.

## Key surfaces

- `team_configs.py` — `/api/v1/team_configs` + `/api/v1/queue_depth`. Coordinator/worker label registry per repo (ADR-0085/0086).
- `hosts.py` — `/api/v1/hosts` + `/api/v1/hosts/bindings`. Host registry and per-label host binding (ADR-0095). See below.
- `tasks.py` — `/api/v1/tasks`. Task CRUD and status.
- `task_executions.py` — `/api/v1/task_executions`. Execution lifecycle (coordinator writes only).
- `task_prs.py` — `/api/v1/task_prs`. PR tracking per task.
- `plans.py` — `/api/v1/plans`. Plan CRUD.
- `onboarding.py` — `/api/v1/onboarding`. Repo config and profile upserts.
- `schedules.py` — `/api/v1/schedules`. Scheduled agent routines.

## Host registry endpoints (`hosts.py`)

The host registry is the durable home (on Core) for named hosts and their
label bindings. Introduced by ADR-0095 — named agents bind to hosts.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/hosts` | Register or refresh a host. Body: `{name, tailnet_addr?}`. `tailnet_addr` is the Tailscale IPv4 (100.x) used by ADR-0097 (Carla) to open the per-host bridge WebSocket. |
| GET | `/api/v1/hosts` | List all registered hosts. |
| POST | `/api/v1/hosts/bindings` | Bind a label to a host. Body: `{label, host, bound_by?}`. |
| GET | `/api/v1/hosts/bindings/{label}` | Get the bound host for a label. Returns 404 if unbound. |
| GET | `/api/v1/hosts/{host}/labels` | List all labels bound to a host. |
| DELETE | `/api/v1/hosts/bindings/{label}` | Remove a label binding. Returns 204 or 404. |

Binding is explicit-only. There is no code path that rebinds a label
automatically on host-unreachable (ADR-0095 Task 3 invariant). The only
write path is `POST /api/v1/hosts/bindings`.

## Recent changes

See `agent-changes/` directory for per-PR change fragments.

## Pitfalls

- Path ordering matters in FastAPI: `/hosts/bindings/{label}` and `/hosts/{host}/labels` share a prefix. FastAPI resolves in declaration order, so `bindings` must be declared before `{host}` in the router if both use `/hosts/...` — the current `hosts.py` router separates them correctly by using full paths.
- Every endpoint commits within the handler. The `get_session` dependency yields an `AsyncSession`; commit is the router's responsibility, not the store's.
- The `bound_at` column refreshes on every `bind_label` upsert (`set_={"bound_at": sa.text("now()")`). This is intentional: a rebind to the same host updates the timestamp.

## Navigation

- **Models:** `models/host.py` — `Host` and `SessionHostBinding` ORM models.
- **Store:** `host_registry_store.py` — `HostRegistryStore` async accessor.
- **Migration:** `alembic/versions/20260813_0100_host_registry.py` — creates `hosts` and `session_host_bindings`.
- **Tests:** `tests/test_host_registry_model.py` — shape + integration tests.
- **Decisions:** ADR-0095 (named agents bind to hosts); ADR-0097 (Carla, tailnet-reachable operator surfaces).
