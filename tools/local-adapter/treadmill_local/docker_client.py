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

    def connect_container_to_network(self, name: str, container: Any) -> None:
        """Attach *container* to the network *name* if not already attached.

        ADR-0064: services that need to talk to BOTH ``treadmill-local``
        (internal-services DNS) and ``treadmill-egress`` (worker traffic)
        get multi-attached via this helper. Idempotent — a no-op when the
        container is already on the network, so callers don't need to
        track attach state.
        """
        network = self._client.networks.get(name)
        container.reload()
        attached = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        if name in attached:
            return
        network.connect(container)
