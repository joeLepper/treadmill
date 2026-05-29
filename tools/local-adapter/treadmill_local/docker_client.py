"""Thin Docker client adapter — dependency-injection seam for egress-proxy wiring.

All egress-proxy interactions (network creation, container spawn, IP lookup) go
through this adapter so tests can swap in a fake without a real Docker daemon.
ADR-0060.
"""

from __future__ import annotations

from typing import Any

import docker


class DockerClientAdapter:
    """Wraps docker-py so egress-proxy callers never import docker directly."""

    def __init__(self, client: Any = None) -> None:
        self._client = client if client is not None else docker.from_env()

    def ensure_network(self, name: str, *, internal: bool = False) -> Any:
        """Return the named network, creating it with an internal bridge if absent."""
        try:
            return self._client.networks.get(name)
        except docker.errors.NotFound:
            return self._client.networks.create(name, driver="bridge", internal=internal)

    def container_running(self, name: str) -> bool:
        """Return True if a container with *name* is currently in the running state."""
        try:
            c = self._client.containers.get(name)
            return c.status == "running"
        except docker.errors.NotFound:
            return False

    def run_container(self, image: str, *, name: str, **kwargs: Any) -> Any:
        """Start a container and return the handle."""
        return self._client.containers.run(image, name=name, **kwargs)

    def get_container_ip(self, container: Any, network_name: str) -> str | None:
        """Return the container's IP on *network_name*, or None if not attached."""
        container.reload()
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        net = networks.get(network_name)
        if net is None:
            return None
        return net.get("IPAddress") or None
