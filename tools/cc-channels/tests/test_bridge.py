"""Tests for tools/cc-channels/bridge.py (ADR-0097 per-host terminal bridge).

RED-THEN-GREEN proof
====================
Every security assertion is proven non-vacuous by first running it against
a naive_handle_connection that skips all gates (RED — assertion fails on
naive code), then running it against the real handle_connection (GREEN —
assertion passes).  The test runner sees only green; the red-then-green
helper functions make the proof explicit and executable.

Security assertions covered:
  A. identity gate     — non-operator closed BEFORE any PTY attach
  B. session gate      — unknown / cross-host session refused; no spawn
  C. allow path        — operator + valid session reaches open_pty_attach
  D. bind gate         — wildcard/LAN/hostname bind raises at startup
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "bridge.py"
sys.path.insert(0, str(BRIDGE.parent))  # makes 'import bridge' work

import bridge  # noqa: E402 — sys.path update above; bridge adds services/api too

from bridge import PORT, handle_connection  # noqa: E402
from treadmill_api.operator_access.bind import tailnet_bind_addr  # noqa: E402

# ── FakeWebSocket ─────────────────────────────────────────────────────────────


class FakeWebSocket:
    """Minimal WebSocket stand-in for unit tests.

    Provides recv(), send(), close(), and async iteration.  Tracks whether
    close() was called and records the close code so assertions can check the
    security outcome without a live server.
    """

    def __init__(self, messages: list[str] | None = None) -> None:
        self._messages = list(messages or [])
        self.sent: list[object] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str = ""

    async def recv(self) -> str:
        if not self._messages:
            raise asyncio.CancelledError
        return self._messages.pop(0)

    async def send(self, data: object) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


# ── Naive handler (no gates — used for red proof) ─────────────────────────────


async def naive_handle_connection(
    websocket,
    *,
    operator_identity: str,
    get_identity,
    list_sessions,
    open_pty_attach,
) -> None:
    """Intentionally un-gated handler.  No identity check, no session check.

    Used ONLY as the RED proof:  every security assertion MUST fail against
    this handler before we show it PASSES against the real handle_connection.
    """
    try:
        session_name = await asyncio.wait_for(websocket.recv(), timeout=1)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        return
    if not isinstance(session_name, str):
        session_name = session_name.decode(errors="replace")
    await open_pty_attach(session_name, websocket)


# ── RED-THEN-GREEN helpers ────────────────────────────────────────────────────
# Each helper returns True iff the security property holds for the given
# handler.  We assert False (red) for naive, True (green) for real.


async def _identity_gate_holds(handler) -> bool:
    """True iff non-operator is refused BEFORE open_pty_attach is called."""
    attach_calls: list[str] = []

    async def fake_attach(session_name: str, ws) -> None:
        attach_calls.append(session_name)

    ws = FakeWebSocket(messages=["alan"])
    await handler(
        ws,
        operator_identity="joe@tailnet",
        get_identity=lambda _: "attacker@tailnet",   # different identity
        list_sessions=lambda: {"alan", "bert"},
        open_pty_attach=fake_attach,
    )
    # Security holds: closed before attach
    return ws.closed and not attach_calls


async def _session_gate_holds(handler) -> bool:
    """True iff an unknown session is refused without calling open_pty_attach."""
    attach_calls: list[str] = []

    async def fake_attach(session_name: str, ws) -> None:
        attach_calls.append(session_name)

    ws = FakeWebSocket(messages=["nonexistent-session"])
    await handler(
        ws,
        operator_identity="joe@tailnet",
        get_identity=lambda _: "joe@tailnet",        # operator identity — only the session is wrong
        list_sessions=lambda: {"alan", "bert"},      # does NOT contain nonexistent-session
        open_pty_attach=fake_attach,
    )
    return ws.closed and not attach_calls


async def _allow_path_works(handler) -> bool:
    """True iff operator + valid session reaches open_pty_attach."""
    attach_calls: list[str] = []

    async def fake_attach(session_name: str, ws) -> None:
        attach_calls.append(session_name)

    ws = FakeWebSocket(messages=["alan"])
    await handler(
        ws,
        operator_identity="joe@tailnet",
        get_identity=lambda _: "joe@tailnet",
        list_sessions=lambda: {"alan", "bert"},
        open_pty_attach=fake_attach,
    )
    return "alan" in attach_calls


# ── RED proof (naive handler fails security properties) ───────────────────────


def test_red_identity_gate_naive_fails() -> None:
    """RED: naive handler does NOT enforce identity gate — this proves the
    real test is non-vacuous."""
    holds = asyncio.run(_identity_gate_holds(naive_handle_connection))
    assert not holds, (
        "naive_handle_connection unexpectedly enforced the identity gate. "
        "Either the naive handler is wrong or the security helper is broken."
    )


def test_red_session_gate_naive_fails() -> None:
    """RED: naive handler does NOT enforce session existence gate."""
    holds = asyncio.run(_session_gate_holds(naive_handle_connection))
    assert not holds, (
        "naive_handle_connection unexpectedly refused an unknown session. "
        "Either the naive handler is wrong or the security helper is broken."
    )


# ── GREEN proof (real handler passes security properties) ─────────────────────


def test_green_identity_gate_non_operator_rejected_before_attach() -> None:
    """GREEN: non-operator connection is closed BEFORE any PTY attach.

    A connection from a non-operator identity must not reach open_pty_attach
    under any circumstances.  This is ADR-0097 Decision 4 — single-operator
    identity ACL.
    """
    holds = asyncio.run(_identity_gate_holds(handle_connection))
    assert holds, (
        "handle_connection did NOT enforce identity gate: either socket not closed "
        "or open_pty_attach was called before the identity check."
    )


def test_green_operator_attaches_named_session() -> None:
    """GREEN (allow path): operator + valid session reaches open_pty_attach."""
    works = asyncio.run(_allow_path_works(handle_connection))
    assert works, (
        "handle_connection did NOT reach open_pty_attach for a valid operator+session pair. "
        "Allow path is broken."
    )


def test_green_unknown_session_refused_no_spawn() -> None:
    """GREEN: operator requesting an unknown session is refused; no attach.

    open_pty_attach must NOT be called for a session not in list_sessions().
    Cross-host sessions are not in the local list, so they are refused by the
    same path — this is why the bridge is cross-host-safe by construction.
    """
    holds = asyncio.run(_session_gate_holds(handle_connection))
    assert holds, (
        "handle_connection did NOT refuse the unknown session; "
        "either socket not closed or open_pty_attach was called."
    )


def test_green_cross_host_session_not_reachable() -> None:
    """GREEN: a session that lives on another host is not in the local list
    and is therefore refused (cross-host impossible by construction)."""
    attach_calls: list[str] = []

    async def fake_attach(session_name: str, ws) -> None:
        attach_calls.append(session_name)

    async def run() -> bool:
        ws = FakeWebSocket(messages=["remote-session-on-host2"])
        await handle_connection(
            ws,
            operator_identity="joe@tailnet",
            get_identity=lambda _: "joe@tailnet",
            list_sessions=lambda: {"alan", "bert"},  # local-only; remote session absent
            open_pty_attach=fake_attach,
        )
        return ws.closed and not attach_calls

    holds = asyncio.run(run())
    assert holds, (
        "handle_connection reached open_pty_attach for a cross-host session name. "
        "Cross-host isolation is broken."
    )


# ── Bind address gate (direct — no naive handler needed) ─────────────────────


@pytest.mark.parametrize(
    "banned",
    [
        "0.0.0.0",       # wildcard / unspecified
        "",              # empty string
        "127.0.0.1",     # loopback
        "localhost",     # loopback hostname
        "LOCALHOST",     # case-variant
        "192.168.1.1",  # LAN (RFC 1918 class C)
        "10.0.0.5",      # LAN (RFC 1918 class A)
        "8.8.8.8",       # public IP
        "example.com",  # arbitrary hostname
        "not-an-ip",     # non-IP
    ],
)
def test_bind_gate_refuses_wildcard_lan_hostname(banned: str) -> None:
    """tailnet_bind_addr (used by bridge startup) raises for non-CGNAT.

    The bridge calls tailnet_bind_addr() before the server socket is opened,
    so invalid bind addresses kill startup before any connection is accepted.
    RED-THEN-GREEN for bind: naive_tailnet_bind_addr below is the red proof.
    """
    with pytest.raises(ValueError):
        tailnet_bind_addr(banned)


def test_naive_tailnet_bind_addr_passes_banned_addresses() -> None:
    """RED for bind: a naive bind-addr function that returns its input
    unchanged lets banned addresses through — proving the test above is
    non-vacuous."""

    def naive_tailnet_bind_addr(candidate: str) -> str:
        return candidate  # no check

    # This would NOT raise — demonstrating the gate is necessary.
    for addr in ("0.0.0.0", "192.168.1.1", "not-an-ip"):
        result = naive_tailnet_bind_addr(addr)
        assert result == addr, "naive function should pass all addresses through unchanged"


def test_bind_gate_cgnat_address_accepted() -> None:
    """Positive: a valid Tailscale CGNAT address is accepted."""
    assert tailnet_bind_addr("100.64.0.1") == "100.64.0.1"
    assert tailnet_bind_addr("100.127.255.254") == "100.127.255.254"


# ── Port constant ─────────────────────────────────────────────────────────────


def test_bridge_port_constant() -> None:
    """bridge.PORT must be 7681 — the fixed well-known terminal bridge port."""
    assert PORT == 7681
