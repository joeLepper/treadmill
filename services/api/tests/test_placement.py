"""Behavioral tests for treadmill_api/placement.py (ADR-0100).

Tests (all sandbox-safe — fake store, no network, no treadmill-local up):
  DEFAULT START: binds to starter's host (TREADMILL_HOST / starter_host param).
  DIRECTED START: registered+reachable host → bound there.
  FAIL-CLOSED:    unregistered host → refused, nothing bound.
  FAIL-CLOSED:    registered but unreachable host → refused, nothing bound.
  FAIL-CLOSED FOIL (red-then-green): naive code silently reroutes; real code refuses.
  STARTER HOST ENV: ValueError when TREADMILL_HOST is absent.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from treadmill_api.placement import PlacementResult, _read_starter_host, place_agent


# ── Fake store ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class _FakeHost:
    name: str
    tailnet_addr: str | None = None


class _SpyStore:
    """Records bind_label calls; returns a configurable host list."""

    def __init__(self, hosts: list[_FakeHost]) -> None:
        self._hosts = hosts
        self.binds: list[tuple[str, str, str | None]] = []

    async def list_hosts(self, session: Any) -> list[_FakeHost]:
        return list(self._hosts)

    async def bind_label(
        self, session: Any, *, label: str, host: str, bound_by: str | None = None
    ) -> None:
        self.binds.append((label, host, bound_by))


# ── DEFAULT START ─────────────────────────────────────────────────────────────


async def test_default_start_binds_starter_host() -> None:
    """Default placement binds to the explicitly supplied starter_host."""
    store = _SpyStore(hosts=[_FakeHost("host-a", "100.64.1.1")])
    result = await place_agent(
        session=None, label="alice", store=store, starter_host="host-a"
    )
    assert result.success is True
    assert result.host == "host-a"
    assert store.binds == [("alice", "host-a", None)]


async def test_default_start_reads_treadmill_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default placement reads TREADMILL_HOST from the environment when no starter_host given."""
    monkeypatch.setenv("TREADMILL_HOST", "rainbow")
    store = _SpyStore(hosts=[])
    result = await place_agent(session=None, label="bob", store=store)
    assert result.success is True
    assert result.host == "rainbow"
    assert store.binds[0][1] == "rainbow"


async def test_default_start_passes_bound_by() -> None:
    """bound_by is forwarded to bind_label on default placement."""
    store = _SpyStore(hosts=[])
    await place_agent(
        session=None, label="carol", store=store,
        starter_host="host-a", bound_by="coordinator-joelepper-treadmill"
    )
    assert store.binds[0][2] == "coordinator-joelepper-treadmill"


# ── DIRECTED START ────────────────────────────────────────────────────────────


async def test_directed_registered_reachable_binds_that_host() -> None:
    """Directed placement to a registered+reachable host binds that host."""
    store = _SpyStore(hosts=[
        _FakeHost("host-a", "100.64.1.1"),
        _FakeHost("host-b", "100.64.2.2"),
    ])
    result = await place_agent(
        session=None, label="dave", store=store,
        directed_host="host-b", starter_host="host-a"
    )
    assert result.success is True
    assert result.host == "host-b"
    assert store.binds == [("dave", "host-b", None)]


async def test_directed_does_not_bind_to_starter_host() -> None:
    """Directed placement ignores starter_host and binds only to directed_host."""
    store = _SpyStore(hosts=[_FakeHost("host-b", "100.64.2.2")])
    result = await place_agent(
        session=None, label="eve", store=store,
        directed_host="host-b", starter_host="host-a"
    )
    assert result.host == "host-b"
    bound_hosts = [b[1] for b in store.binds]
    assert "host-a" not in bound_hosts, "directed placement must not bind to starter_host"


# ── FAIL-CLOSED: unregistered ─────────────────────────────────────────────────


async def test_fail_closed_unregistered_host_refused() -> None:
    """Directed to an unregistered host → refused, nothing bound."""
    store = _SpyStore(hosts=[_FakeHost("host-a", "100.64.1.1")])
    result = await place_agent(
        session=None, label="frank", store=store,
        directed_host="host-missing", starter_host="host-a"
    )
    assert result.success is False
    assert result.host is None
    assert result.reason is not None
    assert "host-missing" in result.reason
    assert store.binds == [], "no binding must be written for an unregistered host"


async def test_fail_closed_unregistered_surfaces_reason() -> None:
    """Failure reason must identify the unregistered host name for operator diagnosis."""
    store = _SpyStore(hosts=[])
    result = await place_agent(
        session=None, label="grace", store=store,
        directed_host="ghost-host", starter_host="host-a"
    )
    assert result.reason is not None, "failure must carry a reason"
    assert "ghost-host" in result.reason


# ── FAIL-CLOSED: unreachable ──────────────────────────────────────────────────


async def test_fail_closed_unreachable_host_refused() -> None:
    """Directed to a registered host with tailnet_addr=None → refused, nothing bound."""
    store = _SpyStore(hosts=[
        _FakeHost("host-a", "100.64.1.1"),
        _FakeHost("host-offline", tailnet_addr=None),  # registered but no Tailscale addr
    ])
    result = await place_agent(
        session=None, label="hannah", store=store,
        directed_host="host-offline", starter_host="host-a"
    )
    assert result.success is False
    assert result.host is None
    assert result.reason is not None
    assert "host-offline" in result.reason
    assert store.binds == [], "no binding must be written for an unreachable host"


async def test_fail_closed_unreachable_does_not_bind_elsewhere() -> None:
    """Directed to an unreachable host must not bind to any alternative."""
    store = _SpyStore(hosts=[
        _FakeHost("host-a", "100.64.1.1"),
        _FakeHost("host-b", "100.64.2.2"),
        _FakeHost("host-offline", tailnet_addr=None),
    ])
    result = await place_agent(
        session=None, label="ivan", store=store,
        directed_host="host-offline", starter_host="host-a"
    )
    assert not result.success
    assert store.binds == [], "must not bind to any host on failure — never rerouted"


# ── FAIL-CLOSED FOIL (red-then-green) ─────────────────────────────────────────


async def _naive_place_directed(
    label: str,
    directed_host: str,
    starter_host: str,
    store: _SpyStore,
    session: Any,
) -> str:
    """Naive placement: silently reroutes to starter_host when directed host fails.

    RED: violates ADR-0100 Decision 4 — reroutes instead of refusing.
    This is the back-door scheduler the ADR explicitly forbids.
    """
    hosts = await store.list_hosts(session)
    host_map = {h.name: h for h in hosts}
    if directed_host in host_map and host_map[directed_host].tailnet_addr is not None:
        target = directed_host
    else:
        target = starter_host  # SILENT REROUTE — violation
    await store.bind_label(session, label=label, host=target, bound_by=None)
    return target


async def test_fail_closed_foil_red_naive_reroutes_on_unregistered() -> None:
    """RED: naive code silently reroutes to starter_host when directed host is unregistered.

    This proves the invariant is non-vacuous — a naive implementation
    violates it by binding somewhere (rerouted) instead of refusing.
    """
    store = _SpyStore(hosts=[_FakeHost("host-a", "100.64.1.1")])
    result_host = await _naive_place_directed(
        label="judy", directed_host="missing-host",
        starter_host="host-a", store=store, session=None,
    )
    # RED: naive code DOES bind — to the starter_host instead of refusing
    assert result_host == "host-a", "red: naive code must reroute to prove the foil"
    assert store.binds != [], "red: naive code must write a binding"


async def test_fail_closed_foil_green_real_refuses_on_unregistered() -> None:
    """GREEN: real code refuses (success=False, no binding) when directed host is unregistered."""
    store = _SpyStore(hosts=[_FakeHost("host-a", "100.64.1.1")])
    result = await place_agent(
        session=None, label="karen", store=store,
        directed_host="missing-host", starter_host="host-a",
    )
    assert result.success is False, "green: real code must refuse, not reroute"
    assert store.binds == [], "green: real code must write NO binding on failure"


async def test_fail_closed_foil_red_naive_reroutes_on_unreachable() -> None:
    """RED: naive code silently reroutes when directed host is unreachable.

    Paired with the green test below: proves non-vacuity for the unreachable case.
    """
    store = _SpyStore(hosts=[
        _FakeHost("host-a", "100.64.1.1"),
        _FakeHost("host-offline", tailnet_addr=None),
    ])
    result_host = await _naive_place_directed(
        label="liam", directed_host="host-offline",
        starter_host="host-a", store=store, session=None,
    )
    assert result_host == "host-a", "red: naive code must reroute to prove the foil"


async def test_fail_closed_foil_green_real_refuses_on_unreachable() -> None:
    """GREEN: real code refuses (success=False, no binding) when directed host is unreachable."""
    store = _SpyStore(hosts=[
        _FakeHost("host-a", "100.64.1.1"),
        _FakeHost("host-offline", tailnet_addr=None),
    ])
    result = await place_agent(
        session=None, label="maya", store=store,
        directed_host="host-offline", starter_host="host-a",
    )
    assert result.success is False, "green: real code must refuse, not reroute"
    assert store.binds == [], "green: real code must write NO binding on failure"


# ── STARTER HOST ENV ──────────────────────────────────────────────────────────


def test_read_starter_host_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_starter_host returns TREADMILL_HOST from env."""
    monkeypatch.setenv("TREADMILL_HOST", "laptop")
    assert _read_starter_host() == "laptop"


def test_read_starter_host_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_starter_host raises ValueError when TREADMILL_HOST is absent."""
    monkeypatch.delenv("TREADMILL_HOST", raising=False)
    with pytest.raises(ValueError, match="TREADMILL_HOST"):
        _read_starter_host()


def test_read_starter_host_raises_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_starter_host raises ValueError when TREADMILL_HOST is empty."""
    monkeypatch.setenv("TREADMILL_HOST", "   ")
    with pytest.raises(ValueError, match="TREADMILL_HOST"):
        _read_starter_host()
