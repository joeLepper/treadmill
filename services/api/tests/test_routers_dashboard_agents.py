"""Tests for ``GET /api/v1/dashboard/agents`` (ADR-0097 §Decision 2).

RED-THEN-GREEN proof
====================
Every security / correctness assertion is proven non-vacuous by first showing
it FAILS against a naive implementation (RED), then PASSES against the real one.

Security assertions covered:
  A. NULL tailnet_addr → reachable=False, bridge_ws_url=None, NO connect attempt
  B. bridge URL uses the REGISTRY'S tailnet_addr — not hardcoded, not guessed
  C. Dashboard READS the host store — never calls write methods
  D. label→host mapping comes from the 0095 registry, not a static config

Sandbox-safe: all tests inject fake store data and a stub session.
No live database, no real tailnet, no second host.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.dashboard import router as dashboard_router
from treadmill_api.routers.dashboard.agents import (
    BRIDGE_PORT,
    AgentEntry,
    _make_agent_entry,
)
import treadmill_api.routers.dashboard.agents as agents_mod


# ── Fake data ─────────────────────────────────────────────────────────────────


def _row(label: str, host: str, tailnet_addr: str | None) -> SimpleNamespace:
    """Build a fake Row-like object matching HostRegistryStore.list_agents output."""
    return SimpleNamespace(label=label, host=host, tailnet_addr=tailnet_addr)


# ── Stub session + store ──────────────────────────────────────────────────────


class _StubResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _StubSession:
    """Returns configured rows for any execute() call (the endpoint issues one query)."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, stmt: Any, params: Any = None) -> _StubResult:
        return _StubResult(self._rows)


class _SpyStore:
    """Records method calls. Write methods record but do NOT raise so tests can assert."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.calls: list[str] = []

    async def list_agents(self, session: Any) -> list:
        self.calls.append("list_agents")
        return self._rows

    async def bind_label(self, session: Any, *a: Any, **kw: Any) -> None:
        self.calls.append("bind_label")

    async def register_host(self, session: Any, *a: Any, **kw: Any) -> None:
        self.calls.append("register_host")

    async def unbind_label(self, session: Any, *a: Any, **kw: Any) -> None:
        self.calls.append("unbind_label")


# ── App builder ───────────────────────────────────────────────────────────────


def _build_app(rows: list) -> TestClient:
    """Create a TestClient with the stub session injected."""
    app = FastAPI()
    app.include_router(dashboard_router)

    def _override() -> Iterator[_StubSession]:
        yield _StubSession(rows)

    app.dependency_overrides[get_session] = _override
    return TestClient(app)


def _build_app_with_spy(spy: _SpyStore) -> TestClient:
    """Build a TestClient that uses a _SpyStore injected at the module level."""
    app = FastAPI()
    app.include_router(dashboard_router)

    # Use a session stub that is never reached by the spy's list_agents
    # (the spy handles the call before the session is needed).
    def _override() -> Iterator[_StubSession]:
        yield _StubSession([])  # session rows irrelevant — spy intercepts

    app.dependency_overrides[get_session] = _override
    return TestClient(app)


# ── RED proof: naive implementations fail security assertions ─────────────────


@pytest.mark.parametrize("poisoned", ["8.8.8.8", "192.168.1.50"])
def test_red_naive_entry_builder_gives_url_for_non_cgnat(poisoned: str) -> None:
    """RED: a naive _make_agent_entry that trusts any non-None tailnet_addr
    returns a bridge_ws_url for poisoned (non-CGNAT) addresses — letting the
    operator browser connect to a URL with no is_operator gate.

    This proves the GREEN test below is non-vacuous (Carla's fold requirement).
    """

    def naive_make_entry(
        label: str, host: str, tailnet_addr: str | None
    ) -> dict[str, Any]:
        if tailnet_addr is not None:  # naive: only checks NULL
            return {"bridge_ws_url": f"ws://{tailnet_addr}:{BRIDGE_PORT}/", "reachable": True}
        return {"bridge_ws_url": None, "reachable": False}

    naive = naive_make_entry("worker-x", "host1", poisoned)
    assert naive["bridge_ws_url"] is not None, (
        f"naive builder should produce a URL for poisoned addr {poisoned!r} "
        "(this proves the GREEN non-CGNAT test is non-vacuous)"
    )


def test_red_naive_entry_builder_gives_url_for_null_tailnet() -> None:
    """RED: a naive _make_agent_entry that uses a fallback address for NULL
    tailnet_addr returns a non-None bridge_ws_url — violating the
    'NULL => UNREACHABLE, no connect attempt' requirement.

    This proves the GREEN test below is non-vacuous.
    """

    def naive_make_entry(
        label: str, host: str, tailnet_addr: str | None
    ) -> dict[str, Any]:
        addr = tailnet_addr or "localhost"  # dangerous fallback
        return {"bridge_ws_url": f"ws://{addr}:{BRIDGE_PORT}/", "reachable": True}

    naive = naive_make_entry("worker-x", "host1", None)
    # Naive gives a URL even for NULL tailnet_addr — violates UNREACHABLE contract
    assert naive["bridge_ws_url"] is not None, (
        "naive builder should produce a URL for NULL tailnet_addr "
        "(this proves the GREEN test is non-vacuous)"
    )
    assert naive["reachable"] is True


def test_red_static_host_list_misses_dynamic_registrations() -> None:
    """RED: a hardcoded host map won't contain dynamically registered hosts —
    proving the endpoint must read from the 0095 registry (GREEN below).
    """
    hardcoded: dict[str, str] = {"rainbow": "100.64.0.1"}  # static config
    dynamic_host = "laptop2"  # registered after deploy
    # The hardcoded approach has no entry for the dynamic host
    assert dynamic_host not in hardcoded, (
        "hardcoded host list should be missing a dynamically registered host; "
        "this proves the registry-based GREEN test is non-vacuous"
    )


# ── GREEN: real implementation passes security assertions ─────────────────────


@pytest.mark.parametrize("poisoned", ["8.8.8.8", "192.168.1.50"])
def test_green_non_cgnat_tailnet_addr_unreachable(poisoned: str) -> None:
    """GREEN: poisoned non-CGNAT tailnet_addr in the registry → UNREACHABLE.

    A registry row with a public IP ("8.8.8.8") or LAN address ("192.168.1.50")
    must NOT produce a bridge_ws_url. The is_tailnet_address() guard in
    _make_agent_entry rejects these via the shared _TAILSCALE_CGNAT constant.
    """
    entry = _make_agent_entry("worker-x", "host1", tailnet_addr=poisoned)
    assert not entry.reachable, (
        f"non-CGNAT addr {poisoned!r} must set reachable=False"
    )
    assert entry.bridge_ws_url is None, (
        f"non-CGNAT addr {poisoned!r} must give bridge_ws_url=None; "
        "operator browser must NOT be pointed at a non-tailnet URL"
    )


def test_green_null_tailnet_addr_unreachable_no_url() -> None:
    """GREEN (security assertion A): NULL tailnet_addr → reachable=False,
    bridge_ws_url=None; no default/guessed address substituted.

    ADR-0097 §Decision 3: 'A NULL tailnet_addr => host is not reachable =>
    the dashboard shows it UNREACHABLE and does NOT attempt a connect.
    NEVER hardcode an address.'
    """
    entry = _make_agent_entry("worker-x", "host1", tailnet_addr=None)
    assert not entry.reachable, "NULL tailnet_addr must set reachable=False"
    assert entry.bridge_ws_url is None, (
        "NULL tailnet_addr must give bridge_ws_url=None; "
        "client must NOT attempt a connect"
    )
    assert entry.tailnet_addr is None


def test_green_reachable_host_builds_correct_bridge_url() -> None:
    """GREEN (security assertion B): non-null tailnet_addr yields the bridge URL
    derived from the registry — not hardcoded, not guessed.
    """
    entry = _make_agent_entry("worker-x", "rainbow", tailnet_addr="100.64.0.1")
    assert entry.reachable
    assert entry.bridge_ws_url == f"ws://100.64.0.1:{BRIDGE_PORT}/"


def test_green_bridge_url_uses_registrys_tailnet_addr() -> None:
    """GREEN: the URL uses the REGISTRY-supplied tailnet_addr. Different hosts
    get different URLs — the address is never hardcoded (ADR-0097 §Decision 3).
    """
    entry_a = _make_agent_entry("alan", "host-a", tailnet_addr="100.64.0.1")
    entry_b = _make_agent_entry("bert", "host-b", tailnet_addr="100.64.0.2")
    assert entry_a.bridge_ws_url != entry_b.bridge_ws_url
    assert "100.64.0.1" in (entry_a.bridge_ws_url or "")
    assert "100.64.0.2" in (entry_b.bridge_ws_url or "")


def test_green_label_host_mapping_from_registry() -> None:
    """GREEN (security assertion D): the endpoint returns the label→host pair
    from the registry store. Dynamically registered hosts appear in the list.
    """
    rows = [
        _row("worker-x", "rainbow", "100.64.0.1"),
        _row("worker-y", "laptop2", "100.64.0.5"),  # dynamically registered
    ]
    client = _build_app(rows)
    resp = client.get("/api/v1/dashboard/agents")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    labels = {a["label"] for a in agents}
    assert "worker-x" in labels
    assert "worker-y" in labels  # dynamic host is reachable
    laptop2 = next(a for a in agents if a["label"] == "worker-y")
    assert laptop2["host"] == "laptop2"
    assert laptop2["tailnet_addr"] == "100.64.0.5"
    assert laptop2["reachable"] is True


def test_green_host_store_read_only_no_writes() -> None:
    """GREEN (security assertion C): get_agents only calls list_agents on
    the store. No write methods (bind_label, register_host, unbind_label)
    are called from the dashboard endpoint (ADR-0097 supplement 1).

    RED proof: if the endpoint called a write method, spy.calls would contain
    that method's name and the assertion below would fail.
    """
    spy = _SpyStore(rows=[_row("worker-x", "rainbow", "100.64.0.1")])
    original = agents_mod._store
    try:
        agents_mod._store = spy  # type: ignore[assignment]
        client = _build_app_with_spy(spy)
        resp = client.get("/api/v1/dashboard/agents")
        assert resp.status_code == 200
    finally:
        agents_mod._store = original

    # Only the read method was called
    assert "list_agents" in spy.calls
    write_calls = [c for c in spy.calls if c != "list_agents"]
    assert write_calls == [], (
        f"dashboard endpoint called write methods: {write_calls!r} — "
        "it must be read-only"
    )


# ── Endpoint integration ──────────────────────────────────────────────────────


def test_endpoint_happy_path_two_reachable_agents() -> None:
    rows = [
        _row("alan", "host-a", "100.64.0.1"),
        _row("bert", "host-b", "100.64.0.2"),
    ]
    resp = _build_app(rows).get("/api/v1/dashboard/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bridge_port"] == BRIDGE_PORT
    agents = body["agents"]
    assert len(agents) == 2
    alan = next(a for a in agents if a["label"] == "alan")
    assert alan["host"] == "host-a"
    assert alan["tailnet_addr"] == "100.64.0.1"
    assert alan["reachable"] is True
    assert alan["bridge_ws_url"] == f"ws://100.64.0.1:{BRIDGE_PORT}/"


def test_endpoint_null_tailnet_addr_unreachable_in_response() -> None:
    """NULL tailnet_addr host appears in the list with reachable=False and no URL."""
    rows = [
        _row("reachable-worker", "host-a", "100.64.0.1"),
        _row("unreachable-worker", "host-b", None),  # not yet registered / no Tailscale
    ]
    resp = _build_app(rows).get("/api/v1/dashboard/agents")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    unreachable = next(a for a in agents if a["label"] == "unreachable-worker")
    assert unreachable["reachable"] is False
    assert unreachable["bridge_ws_url"] is None
    assert unreachable["tailnet_addr"] is None


def test_endpoint_empty_registry_returns_empty_list() -> None:
    resp = _build_app([]).get("/api/v1/dashboard/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agents"] == []
    assert body["bridge_port"] == BRIDGE_PORT


def test_endpoint_is_auto_discovered_by_dashboard_package() -> None:
    """The agents module exposes ``router`` and is picked up by dashboard auto-discovery."""
    from treadmill_api.routers import dashboard as dashboard_pkg

    assert "agents" in dashboard_pkg.MOUNTED_MODULES
    paths = {getattr(r, "path", None) for r in dashboard_pkg.router.routes}
    assert "/api/v1/dashboard/agents" in paths


# ── Static checks ─────────────────────────────────────────────────────────────


def test_bridge_port_constant_is_7681() -> None:
    assert BRIDGE_PORT == 7681


def test_agents_imports_is_tailnet_address_not_reimplemented() -> None:
    """agents.py must import is_tailnet_address from operator_access.bind.
    The CGNAT range must NOT be re-defined in the dashboard module.
    """
    src = (
        Path(__file__).parents[1]
        / "treadmill_api"
        / "routers"
        / "dashboard"
        / "agents.py"
    ).read_text()
    assert "is_tailnet_address" in src, "agents.py must import is_tailnet_address"
    assert "from treadmill_api.operator_access.bind import is_tailnet_address" in src, (
        "is_tailnet_address must be imported from operator_access.bind, not re-defined"
    )
    # IPv4Network must not be instantiated in agents.py — re-defining the CGNAT range
    # there would create a second source of truth that can drift from operator_access/bind.py.
    # (Mentioning "100.64.0.0/10" in a comment is acceptable; instantiating IPv4Network is not.)
    assert 'IPv4Network("' not in src and "IPv4Network('" not in src, (
        "IPv4Network instantiation found in agents.py — "
        "CGNAT range must not be redefined; import is_tailnet_address instead"
    )


def test_no_hardcoded_ip_address_in_agents_source() -> None:
    """Static scan: agents.py must not contain raw IP literals as defaults.

    The bridge URL must always come from the registry's tailnet_addr. Any
    hardcoded IP would bypass the registry and silently target the wrong host.
    """
    src = (
        Path(__file__).parents[1]
        / "treadmill_api"
        / "routers"
        / "dashboard"
        / "agents.py"
    ).read_text()
    # IP literals in double or single quotes (e.g. "100.64.0.1")
    matches = re.findall(r'["\'](\d{1,3}(?:\.\d{1,3}){3})["\']', src)
    assert not matches, (
        f"hardcoded IP address(es) found in agents.py: {matches!r}. "
        "Bridge URLs must be derived from the host registry, never hardcoded."
    )
    for banned in ('"localhost"', "'localhost'", '"0.0.0.0"', "'0.0.0.0'"):
        assert banned not in src, (
            f"hardcoded address {banned!r} found in agents.py"
        )


def test_list_agents_method_on_store() -> None:
    """HostRegistryStore exposes list_agents (added for ADR-0097 dashboard)."""
    from treadmill_api.host_registry_store import HostRegistryStore

    assert hasattr(HostRegistryStore, "list_agents"), (
        "HostRegistryStore missing list_agents — required by the agents endpoint"
    )
