"""Agent placement — default-local and directed, fail-closed (ADR-0100).

Policy (ADR-0100 Decisions 1–4):
  1. DEFAULT: new agent's label→host binding goes to the starter's host (TREADMILL_HOST).
  2. DIRECTED: binds a named host that must be registered AND reachable (non-NULL tailnet_addr).
  3. No scheduler / allocator / reconciler. Placement is written once, at start.
  4. FAIL-CLOSED: an unregistered or unreachable directed host REFUSES the start and surfaces
     the reason. It is NEVER silently rerouted to another host.

Placement uses ADR-0095's HostRegistryStore exclusively — bind_label for writes,
list_hosts for directed-host validation. No parallel host store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

STARTER_HOST_ENV = "TREADMILL_HOST"


@dataclass(frozen=True)
class PlacementResult:
    """Outcome of one placement attempt.

    On failure (success=False) the caller MUST exit non-zero and surface
    ``reason`` to the operator. The start is not attempted, not rerouted.
    """

    success: bool
    host: str | None    # the host bound when success=True; None on failure
    reason: str | None  # human-readable failure description; None on success


def _read_starter_host(env: dict[str, str] | None = None) -> str:
    """Return the value of TREADMILL_HOST from the environment.

    Raises ValueError when the variable is absent or empty — callers must not
    proceed without a known starter host.
    """
    env = env if env is not None else os.environ
    host = env.get(STARTER_HOST_ENV, "").strip()
    if not host:
        raise ValueError(
            f"{STARTER_HOST_ENV} is not set — cannot determine the starter's host"
        )
    return host


async def place_agent(
    session: Any,
    label: str,
    store: Any,
    *,
    directed_host: str | None = None,
    starter_host: str | None = None,
    bound_by: str | None = None,
) -> PlacementResult:
    """Write the label→host binding for a new agent.

    Default (directed_host=None):
        Binds label to ``starter_host`` (or reads TREADMILL_HOST from env).
        Always succeeds when the env var is set.

    Directed (directed_host=<name>):
        Checks that the named host is registered in the host registry AND has a
        non-NULL tailnet_addr (reachable via Tailscale). On either failure the
        start is REFUSED — PlacementResult(success=False). The binding is never
        written; the label is never placed on a fallback host.

    Args:
        session:       Async DB session owned by the caller.
        label:         The new agent's label to bind.
        store:         HostRegistryStore (or test stub) — provides list_hosts + bind_label.
        directed_host: Named host override. None → default-local.
        starter_host:  The starting agent's host. None → read TREADMILL_HOST.
        bound_by:      Optional actor label recorded on the binding row.

    Returns:
        PlacementResult(success=True, host=<bound_host>)   on success
        PlacementResult(success=False, reason=<why>)        on refusal
    """
    if directed_host is None:
        target = starter_host or _read_starter_host()
        await store.bind_label(session, label=label, host=target, bound_by=bound_by)
        return PlacementResult(success=True, host=target, reason=None)

    # Directed placement: validate registration then reachability before binding.
    hosts = await store.list_hosts(session)
    host_map = {h.name: h for h in hosts}

    if directed_host not in host_map:
        return PlacementResult(
            success=False,
            host=None,
            reason=(
                f"directed host {directed_host!r} is not registered in the host registry"
            ),
        )

    host_row = host_map[directed_host]
    if host_row.tailnet_addr is None:
        return PlacementResult(
            success=False,
            host=None,
            reason=(
                f"directed host {directed_host!r} is registered but has no Tailscale "
                f"address (tailnet_addr is NULL) — start refused"
            ),
        )

    await store.bind_label(session, label=label, host=directed_host, bound_by=bound_by)
    return PlacementResult(success=True, host=directed_host, reason=None)
