"""``GET /api/v1/dashboard/agents`` — multi-host agent list (ADR-0097 §Decision 2).

Lists all session agents by (label, host) across enrolled hosts. Resolves
host → Tailscale address via ADR-0095's host registry (HostRegistryStore).
Returns the bridge WebSocket URL for reachable hosts and marks UNREACHABLE when
tailnet_addr is NULL (host not registered or no Tailscale address yet).

Security contract (ADR-0097):
- This endpoint READS the host registry. It NEVER writes the host store.
- The bridge WebSocket URL is derived from the registry's tailnet_addr.
  No address is hardcoded; a NULL tailnet_addr produces bridge_ws_url=None
  and reachable=False — the client must NOT attempt a connect.
- The dashboard ADDRESSES the host's bridge (ADR-0097 Task 2, bridge.py).
  This endpoint does NOT attach, proxy, or forward terminal traffic; it only
  returns addressing information for the frontend to use.
- The resulting bridge URL still passes through the bridge's is_operator gate
  (ADR-0097 Task 2) — the dashboard introduces no new unauthenticated path.

See ADR-0097 §Decision 2 (one dashboard, every host) and §Decision 3 (bind
to tailnet interface only — NEVER hardcode an address).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.host_registry_store import HostRegistryStore
from treadmill_api.operator_access.bind import is_tailnet_address  # CGNAT guard

router = APIRouter()

# Fixed well-known port for the per-host terminal bridge.
# Source: tools/cc-channels/bridge.py PORT = 7681 (ADR-0097, task 31c4072b).
BRIDGE_PORT = 7681

_store = HostRegistryStore()


# ── Response shapes ───────────────────────────────────────────────────────────


class AgentEntry(BaseModel):
    label: str
    host: str
    tailnet_addr: str | None  # from ADR-0095 hosts registry; None = not reachable
    bridge_ws_url: str | None  # ws://<tailnet_addr>:<BRIDGE_PORT>/ or None
    reachable: bool  # False iff tailnet_addr is None


class AgentsResponse(BaseModel):
    agents: list[AgentEntry]
    bridge_port: int  # always BRIDGE_PORT; surfaced so the client doesn't hardcode it


# ── Pure addressing helper ────────────────────────────────────────────────────


def _make_agent_entry(label: str, host: str, tailnet_addr: str | None) -> AgentEntry:
    """Build one AgentEntry. CGNAT-allowlist guard before any URL is constructed.

    A bridge WebSocket URL is built ONLY when is_tailnet_address() confirms the
    registry-supplied tailnet_addr is a valid 100.64.0.0/10 CGNAT address.
    NULL, LAN, public, hostname, IPv6 — all render UNREACHABLE with no URL.
    The CGNAT range is defined once in operator_access.bind; no literal here.
    """
    if tailnet_addr is None or not is_tailnet_address(tailnet_addr):
        bridge_ws_url: str | None = None  # UNREACHABLE — do not connect
        reachable = False
    else:
        bridge_ws_url = f"ws://{tailnet_addr}:{BRIDGE_PORT}/"
        reachable = True
    return AgentEntry(
        label=label,
        host=host,
        tailnet_addr=tailnet_addr,
        bridge_ws_url=bridge_ws_url,
        reachable=reachable,
    )


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/agents", response_model=AgentsResponse)
async def get_agents(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentsResponse:
    """List all session agents by (label, host) with bridge addressing.

    Reads ADR-0095's host registry (HostRegistryStore.list_agents) via the
    existing store — does NOT write to the host store. NULL tailnet_addr hosts
    are returned with reachable=False and bridge_ws_url=None (UNREACHABLE).
    No address is hardcoded; every URL is derived from the registry
    (ADR-0097 §Decision 3).
    """
    raw = await _store.list_agents(session)
    agents = [_make_agent_entry(row.label, row.host, row.tailnet_addr) for row in raw]
    return AgentsResponse(agents=agents, bridge_port=BRIDGE_PORT)
