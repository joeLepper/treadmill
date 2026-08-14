#!/usr/bin/env python3
"""Per-host WebSocket terminal bridge (ADR-0097, plan 915fdea0).

Binds ONLY to the host's Tailscale address (via treadmill_api.operator_access.bind),
gates EVERY connection with Tailscale identity (via treadmill_api.operator_access.acl),
and attaches ONLY to locally-named existing tmux sessions. One bridge per host;
no cross-host calls.

SECURITY INVARIANTS (fail-closed, all from ADR-0097):
- Wildcard / LAN / hostname bind address -> ValueError at startup; bridge refuses.
- Every WebSocket connection is checked; non-operators are closed BEFORE any PTY.
- Only sessions listed by local tmux are reachable; no shell is ever spawned.
- One bridge serves only its local host's sessions; it makes NO cross-host calls.

Usage (production):
    bridge.py [--host TAILSCALE_IP] [--port PORT] [--operator-identity IDENTITY]

Environment fallbacks (used when flags are absent):
    TREADMILL_HOST                  local Tailscale address
    TREADMILL_OPERATOR_IDENTITY     configured operator Tailscale login
    TREADMILL_BRIDGE_PORT           port  (default 7681)
    TREADMILL_REPO_DIR              repo root for treadmill_api import path fallback
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

# ── treadmill_api import path ────────────────────────────────────────────────
# bridge.py lives at tools/cc-channels/bridge.py; treadmill_api is at
# services/api/ relative to the repo root (two levels up from here).
_HERE = os.path.dirname(os.path.abspath(__file__))
_API_PKG = os.path.normpath(os.path.join(_HERE, "..", "..", "services", "api"))
if _API_PKG not in sys.path:
    sys.path.insert(0, _API_PKG)

# REUSE, do not reimplement (ADR-0097 supplement — Donna grep gate):
# is_operator and tailnet_bind_addr live in treadmill_api.operator_access.
from treadmill_api.operator_access.acl import is_operator  # noqa: E402
from treadmill_api.operator_access.bind import tailnet_bind_addr  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────

PORT = 7681  # Fixed well-known port for the Treadmill terminal bridge.

log = logging.getLogger(__name__)

# ── Core handler (injectable for tests) ─────────────────────────────────────


async def handle_connection(
    websocket,
    *,
    operator_identity: str,
    get_identity,       # (websocket) -> str | None
    list_sessions,      # () -> set[str]  — must return ONLY local session names
    open_pty_attach,    # async (session_name, websocket) -> None
) -> None:
    """Handle one WebSocket connection (all security logic here).

    Step 1 — Identity gate: extract Tailscale identity and check is_operator().
              Non-operator connections are closed IMMEDIATELY, before any recv().
    Step 2 — Session name: receive the target session name from the client.
    Step 3 — Existence check: validate against list_sessions() (local-only).
              Unknown / cross-host sessions are refused; no shell is spawned.
    Step 4 — Attach: call open_pty_attach() to bridge the PTY to the socket.
              Detach (client disconnect) leaves the session running.

    This function never spawns a shell and never accepts commands as input;
    the client names an existing session and the bridge connects to its PTY.
    """
    # ── Step 1: gate every connection on operator identity ────────────────
    identity = get_identity(websocket)
    if not is_operator(identity, operator_identity=operator_identity):
        log.warning("rejected connection: identity=%r is not the operator", identity)
        await websocket.close(1008, "unauthorized")
        return

    # ── Step 2: receive session name ──────────────────────────────────────
    try:
        session_name = await asyncio.wait_for(websocket.recv(), timeout=10)
    except (asyncio.TimeoutError, Exception):
        await websocket.close(1008, "timeout waiting for session name")
        return
    if not isinstance(session_name, str):
        session_name = session_name.decode(errors="replace")

    # ── Step 3: validate session exists locally ───────────────────────────
    # list_sessions() returns ONLY local tmux sessions; requesting a session
    # on another host is indistinguishable from requesting a missing one,
    # making cross-host attach impossible by construction.
    if session_name not in list_sessions():
        log.warning("rejected session=%r: not in local session list", session_name)
        await websocket.close(1011, "unknown session")
        return

    # ── Step 4: attach PTY ───────────────────────────────────────────────
    log.info("operator %r attaching to session %r", identity, session_name)
    await open_pty_attach(session_name, websocket)


# ── Production implementations ───────────────────────────────────────────────


def _tailscale_identity(websocket) -> str | None:
    """Resolve caller Tailscale identity via the tailscale whois CLI."""
    remote = getattr(websocket, "remote_address", None)
    if not remote:
        return None
    ip, port = remote[0], remote[1]
    try:
        import json
        result = subprocess.run(
            ["tailscale", "whois", f"{ip}:{port}", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data.get("UserProfile", {}).get("LoginName")
    except Exception:
        return None


def _list_local_sessions() -> set[str]:
    """Return session names from the local tmux server (local-only, never cross-host)."""
    result = subprocess.run(
        ["tmux", "ls", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    return set(result.stdout.strip().splitlines())


async def _attach_tmux_session(session_name: str, websocket) -> None:
    """Bridge a local tmux session PTY to a WebSocket.

    The named session is validated BEFORE this is called. This function
    only runs `tmux attach-session -t <session_name>`; it never spawns a
    shell and never interprets client messages as commands.
    Detach closes the socket and leaves the session running (ADR-0097 §5).
    """
    import pty
    import os as _os

    loop = asyncio.get_running_loop()
    master_fd, slave_fd = pty.openpty()
    proc = await asyncio.create_subprocess_exec(
        "tmux", "attach-session", "-t", session_name,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
    )
    _os.close(slave_fd)

    async def pty_to_ws() -> None:
        while True:
            try:
                data = await loop.run_in_executor(None, _os.read, master_fd, 4096)
                if data:
                    await websocket.send(data)
            except OSError:
                break

    async def ws_to_pty() -> None:
        async for msg in websocket:
            payload = msg if isinstance(msg, bytes) else msg.encode()
            _os.write(master_fd, payload)

    tasks = [
        asyncio.ensure_future(pty_to_ws()),
        asyncio.ensure_future(ws_to_pty()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        try:
            _os.close(master_fd)
        except OSError:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        proc.terminate()


# ── Entry point ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    import argparse
    import websockets.server  # type: ignore[import]

    parser = argparse.ArgumentParser(description="Treadmill terminal bridge (ADR-0097)")
    parser.add_argument("--host", default=os.environ.get("TREADMILL_HOST", ""))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("TREADMILL_BRIDGE_PORT", str(PORT))))
    parser.add_argument("--operator-identity",
                        default=os.environ.get("TREADMILL_OPERATOR_IDENTITY", ""))
    args = parser.parse_args(argv)

    if not args.operator_identity:
        print("bridge: TREADMILL_OPERATOR_IDENTITY is required", file=sys.stderr)
        sys.exit(1)

    # Validate bind address (fail-closed: wildcard/LAN/hostname raises).
    bind_addr = tailnet_bind_addr(args.host)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s bridge %(levelname)s %(message)s")
    operator_identity = args.operator_identity

    async def handler(websocket) -> None:
        await handle_connection(
            websocket,
            operator_identity=operator_identity,
            get_identity=_tailscale_identity,
            list_sessions=_list_local_sessions,
            open_pty_attach=_attach_tmux_session,
        )

    async def serve() -> None:
        async with websockets.server.serve(handler, bind_addr, args.port):
            log.info("bridge listening on %s:%d", bind_addr, args.port)
            await asyncio.Future()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
