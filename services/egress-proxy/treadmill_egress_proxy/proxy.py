"""HTTP CONNECT proxy with per-worker allowlist enforcement (ADR-0060)."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
import sys
from datetime import datetime, timezone

from .config import ConfigStore, WorkerAllowlist

_TUNNEL_CHUNK = 65536


def _log(
    worker_ip: str,
    hostname: str,
    phase: str,
    decision: str,
    reason: str,
) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker_ip": worker_ip,
        "hostname": hostname,
        "phase": phase,
        "decision": decision,
        "reason": reason,
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _extract_proxy_auth_password(headers: dict[str, str]) -> str | None:
    raw = headers.get("proxy-authorization")
    if not raw:
        return None
    parts = raw.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1]).decode()
    except Exception:
        return None
    if ":" not in decoded:
        return None
    return decoded.split(":", 1)[1]


def _matches(hostname: str, patterns: list[str]) -> bool:
    """Glob match — exact OR wildcard. Entries like ``*.github.com`` in the
    allowlist match ``api.github.com`` / ``raw.githubusercontent.com`` /
    etc. via ``fnmatch``; exact entries like ``github.com`` still match
    only that string. ADR-0060: the allowlist was authored with wildcard
    syntax intent (``*.githubusercontent.com``); plain ``in`` checks
    silently never matched and the 2026-06-03 wedge surfaced it when
    git clone reached bare ``github.com``."""
    return any(fnmatch.fnmatchcase(hostname, p) for p in patterns)


def _decide(
    hostname: str,
    allowlist: WorkerAllowlist | None,
    password: str | None,
    worker_ip: str,
) -> tuple[bool, str, str]:
    """Return (allowed, phase, reason)."""
    if allowlist is None:
        return False, "unknown", "no_config_for_worker"

    if _matches(hostname, allowlist.always_allowed):
        return True, "always", "always_allowed"

    if _matches(hostname, allowlist.install_allowed):
        if password is None:
            return False, "install", "credential_required"
        if _sha256_hex(password) == allowlist.install_credential_hash:
            return True, "install", "install_allowed"
        return False, "install", "credential_mismatch"

    return False, "none", "hostname_not_allowed"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(_TUNNEL_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    store: ConfigStore,
) -> None:
    try:
        peername = writer.get_extra_info("peername")
        worker_ip = peername[0] if peername else "unknown"

        # Read request line + headers
        first_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not first_line:
            writer.close()
            return
        request_line = first_line.decode(errors="replace").strip()

        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            decoded = line.decode(errors="replace").strip()
            if not decoded:
                break
            if ":" in decoded:
                key, _, val = decoded.partition(":")
                headers[key.strip().lower()] = val.strip()

        # Parse CONNECT target
        parts = request_line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        host_port = parts[1]
        if ":" in host_port:
            hostname, _, port_str = host_port.rpartition(":")
            try:
                port = int(port_str)
            except ValueError:
                port = 443
        else:
            hostname = host_port
            port = 443

        password = _extract_proxy_auth_password(headers)
        allowlist = store.get(worker_ip)

        allowed, phase, reason = _decide(hostname, allowlist, password, worker_ip)
        _log(worker_ip, hostname, phase, "allow" if allowed else "deny", reason)

        if not allowed:
            body = f"Forbidden: {reason}\r\n".encode()
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            return

        # Open upstream connection and tunnel
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(hostname, port), timeout=10
            )
        except Exception as exc:
            body = f"Connection failed: {exc}\r\n".encode()
            writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        await asyncio.gather(
            _pipe(reader, up_writer),
            _pipe(up_reader, writer),
            return_exceptions=True,
        )
    except Exception:
        try:
            writer.close()
        except Exception:
            pass


async def run_proxy(host: str, port: int, store: ConfigStore) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, store),
        host=host,
        port=port,
    )
    async with server:
        await server.serve_forever()
