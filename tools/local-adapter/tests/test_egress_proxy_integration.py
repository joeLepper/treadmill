"""End-to-end allow/deny smoke for the egress proxy (ADR-0060 Step 3d).

Spawns the proxy in-process on an ephemeral loopback port and a fake
upstream HTTP server on a second ephemeral port. Drives real CONNECT
requests through it and asserts the decision matrix:

  - install_allowed host + correct credential  -> 200 + tunnel works
  - install_allowed host without credential    -> 403 (install-only gate)
  - unknown host without credential            -> 403
  - unknown host even with credential          -> 403 (credential does
    not override the allowlist)

No docker, no real network — pytest-asyncio + loopback sockets only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator

import pytest

from treadmill_egress_proxy.config import ConfigStore
from treadmill_egress_proxy.proxy import _handle

INSTALL_TOKEN = "integration-install-token-xyz789"
INSTALL_HASH = hashlib.sha256(INSTALL_TOKEN.encode()).hexdigest()

# Hostname that appears only in always_allowed; the proxy never has to
# open a TCP connection to it because every test that uses it asserts a
# deny path or never targets it.
ALWAYS_ONLY_HOST = "always-only.example"

# Hostname that is in NEITHER allowlist — used to exercise the
# hostname_not_allowed deny path with and without a credential.
UNKNOWN_HOST = "denied.example"

# The install-allowed host the proxy must actually dial when the allow
# path is exercised. Using "localhost" keeps the test honest end-to-end:
# the proxy resolves it via getaddrinfo and opens a real TCP socket to
# the fake upstream below.
INSTALL_HOST = "localhost"


def _basic_auth(password: str) -> str:
    encoded = base64.b64encode(f"install:{password}".encode()).decode()
    return f"Basic {encoded}"


async def _send_connect(
    proxy_port: int,
    target_host: str,
    target_port: int,
    *,
    password: str | None,
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a TCP connection to the proxy and send a CONNECT request.

    Returns the status line and the (still open) reader/writer so the
    caller can drive the tunnel when the proxy returns 200.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    lines = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
    ]
    if password is not None:
        lines.append(f"Proxy-Authorization: {_basic_auth(password)}")
    lines += ["", ""]
    writer.write("\r\n".join(lines).encode())
    await writer.drain()
    status = await asyncio.wait_for(reader.readline(), timeout=5)
    return status, reader, writer


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


@pytest.fixture
async def upstream_port(tmp_path) -> AsyncIterator[int]:
    """Fake HTTPS-substrate upstream listening on an ephemeral 127.0.0.1 port.

    Speaks plain HTTP/1.1 — the proxy only opens a TCP tunnel, so the
    upstream payload doesn't need to be TLS for the smoke to be useful.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
        except Exception:
            pass
        body = b"upstream-ok"
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass


@pytest.fixture
async def proxy_port(tmp_path) -> AsyncIterator[int]:
    """Spawn the egress proxy in a background asyncio task.

    Writes a synthetic per-worker allowlist matching the shape the
    autoscaler produces in production (ADR-0060): keyed by worker_ip
    = 127.0.0.1, with INSTALL_HOST in install_allowed only (so the
    install-credential gate is what we're actually testing) and an
    unrelated host in always_allowed (asserting always_allowed and
    install_allowed are independent allowlists, not interchangeable).
    """
    config_dir = tmp_path / "egress-proxy-config"
    config_dir.mkdir()
    (config_dir / "127.0.0.1.json").write_text(
        json.dumps(
            {
                "worker_ip": "127.0.0.1",
                "always_allowed": [ALWAYS_ONLY_HOST],
                "install_allowed": [INSTALL_HOST],
                "install_credential_hash": INSTALL_HASH,
            }
        )
    )
    store = ConfigStore(config_dir)
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, store), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        yield port
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass


# ── allow path: install credential + install_allowed host → 200 + tunnel ─────

async def test_install_allowed_with_credential_succeeds_end_to_end(
    proxy_port: int, upstream_port: int
) -> None:
    status, reader, writer = await _send_connect(
        proxy_port, INSTALL_HOST, upstream_port, password=INSTALL_TOKEN
    )
    try:
        assert status.startswith(b"HTTP/1.1 200"), status

        # Consume the empty CRLF terminating the CONNECT response so
        # subsequent reads see only tunneled upstream bytes.
        sep = await asyncio.wait_for(reader.readline(), timeout=5)
        assert sep == b"\r\n", sep

        # Drive the tunnel: a real GET should reach the upstream and the
        # upstream's response should come back to us through the proxy.
        writer.write(b"GET / HTTP/1.1\r\nHost: " + INSTALL_HOST.encode() + b"\r\n\r\n")
        await writer.drain()

        # Upstream sets Connection: close, so reading until EOF is
        # bounded — and avoids the early-return race of read(n).
        response = await asyncio.wait_for(reader.read(), timeout=5)
        assert b"200 OK" in response, response
        assert b"upstream-ok" in response, response
    finally:
        await _close(writer)


# ── install_allowed gate: missing credential → 403 ───────────────────────────

async def test_install_allowed_without_credential_denied(
    proxy_port: int, upstream_port: int
) -> None:
    """INSTALL_HOST is in install_allowed only — no credential ⇒ 403, and
    the proxy must never open the upstream socket on a deny."""
    status, _, writer = await _send_connect(
        proxy_port, INSTALL_HOST, upstream_port, password=None
    )
    try:
        assert b"403" in status, status
    finally:
        await _close(writer)


# ── unknown host: denied in both phases ──────────────────────────────────────

async def test_unknown_host_without_credential_denied(
    proxy_port: int, upstream_port: int
) -> None:
    status, _, writer = await _send_connect(
        proxy_port, UNKNOWN_HOST, upstream_port, password=None
    )
    try:
        assert b"403" in status, status
    finally:
        await _close(writer)


async def test_unknown_host_with_credential_still_denied(
    proxy_port: int, upstream_port: int
) -> None:
    """A valid install credential does not override the allowlist — a
    host that is in neither always_allowed nor install_allowed is denied
    even when the credential is correct."""
    status, _, writer = await _send_connect(
        proxy_port, UNKNOWN_HOST, upstream_port, password=INSTALL_TOKEN
    )
    try:
        assert b"403" in status, status
    finally:
        await _close(writer)
