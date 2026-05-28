"""Tests for the CONNECT proxy decision paths (no real external network)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from treadmill_egress_proxy.config import ConfigStore
from treadmill_egress_proxy.proxy import _handle


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


INSTALL_TOKEN = "test-install-token-abc123"
INSTALL_HASH = _sha256(INSTALL_TOKEN)

ALWAYS_HOST = "always.example"
INSTALL_HOST = "install.example"
UNKNOWN_HOST = "unknown.example"


def _basic_auth(password: str) -> str:
    encoded = base64.b64encode(f"install:{password}".encode()).decode()
    return f"Basic {encoded}"


async def _connect(proxy_port: int, target: str, port: int = 443, password: str | None = None) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    lines = [f"CONNECT {target}:{port} HTTP/1.1", f"Host: {target}:{port}"]
    if password is not None:
        lines.append(f"Proxy-Authorization: {_basic_auth(password)}")
    lines += ["", ""]
    writer.write("\r\n".join(lines).encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return response


async def _make_proxy(tmp_path, allowlist_data: dict) -> tuple[int, asyncio.Server]:
    (tmp_path / "worker.json").write_text(json.dumps(allowlist_data))
    store = ConfigStore(tmp_path)
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, store), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    return port, server


@pytest.fixture
async def upstream_server():
    """Minimal echo server that accepts a connection."""
    async def handle(reader, writer):
        try:
            writer.write(b"UPSTREAM_OK")
            await writer.drain()
            await asyncio.wait_for(reader.read(100), timeout=2)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    _, port = server.sockets[0].getsockname()
    yield port
    server.close()
    await server.wait_closed()


# ── always-allowed allow path ────────────────────────────────────────────────

async def test_always_allowed_returns_200(tmp_path, upstream_server):
    port, server = await _make_proxy(tmp_path, {
        "worker_ip": "127.0.0.1",
        "always_allowed": ["127.0.0.1"],
        "install_allowed": [],
        "install_credential_hash": "a" * 64,
    })
    try:
        resp = await _connect(port, "127.0.0.1", upstream_server)
        assert resp.startswith(b"HTTP/1.1 200")
    finally:
        server.close()
        await server.wait_closed()


# ── install-allowed deny without credential ──────────────────────────────────

async def test_install_allowed_deny_no_credential(tmp_path):
    port, server = await _make_proxy(tmp_path, {
        "worker_ip": "127.0.0.1",
        "always_allowed": [],
        "install_allowed": [INSTALL_HOST],
        "install_credential_hash": INSTALL_HASH,
    })
    try:
        resp = await _connect(port, INSTALL_HOST)
        assert b"403" in resp
    finally:
        server.close()
        await server.wait_closed()


# ── install-allowed allow with correct credential ────────────────────────────

async def test_install_allowed_allow_correct_credential(tmp_path, upstream_server):
    port, server = await _make_proxy(tmp_path, {
        "worker_ip": "127.0.0.1",
        "always_allowed": [],
        "install_allowed": ["127.0.0.1"],
        "install_credential_hash": INSTALL_HASH,
    })
    try:
        resp = await _connect(port, "127.0.0.1", upstream_server, password=INSTALL_TOKEN)
        assert resp.startswith(b"HTTP/1.1 200")
    finally:
        server.close()
        await server.wait_closed()


# ── install-allowed deny with wrong credential ───────────────────────────────

async def test_install_allowed_deny_wrong_credential(tmp_path):
    port, server = await _make_proxy(tmp_path, {
        "worker_ip": "127.0.0.1",
        "always_allowed": [],
        "install_allowed": [INSTALL_HOST],
        "install_credential_hash": INSTALL_HASH,
    })
    try:
        resp = await _connect(port, INSTALL_HOST, password="wrong-password")
        assert b"403" in resp
    finally:
        server.close()
        await server.wait_closed()


# ── unknown hostname deny ────────────────────────────────────────────────────

async def test_unknown_hostname_deny(tmp_path):
    port, server = await _make_proxy(tmp_path, {
        "worker_ip": "127.0.0.1",
        "always_allowed": [ALWAYS_HOST],
        "install_allowed": [INSTALL_HOST],
        "install_credential_hash": INSTALL_HASH,
    })
    try:
        resp = await _connect(port, UNKNOWN_HOST)
        assert b"403" in resp
    finally:
        server.close()
        await server.wait_closed()


async def test_unknown_hostname_deny_with_credential(tmp_path):
    port, server = await _make_proxy(tmp_path, {
        "worker_ip": "127.0.0.1",
        "always_allowed": [ALWAYS_HOST],
        "install_allowed": [INSTALL_HOST],
        "install_credential_hash": INSTALL_HASH,
    })
    try:
        resp = await _connect(port, UNKNOWN_HOST, password=INSTALL_TOKEN)
        assert b"403" in resp
    finally:
        server.close()
        await server.wait_closed()


# ── malformed request ────────────────────────────────────────────────────────

async def test_non_connect_method_returns_400(tmp_path):
    port, server = await _make_proxy(tmp_path, {
        "worker_ip": "127.0.0.1",
        "always_allowed": [],
        "install_allowed": [],
        "install_credential_hash": "a" * 64,
    })
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        await writer.drain()
        resp = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        assert b"400" in resp
    finally:
        server.close()
        await server.wait_closed()


# ── no config for worker ─────────────────────────────────────────────────────

async def test_no_config_for_worker_returns_403(tmp_path):
    store = ConfigStore(tmp_path)  # empty directory
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, store), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    try:
        resp = await _connect(port, ALWAYS_HOST)
        assert b"403" in resp
    finally:
        server.close()
        await server.wait_closed()
