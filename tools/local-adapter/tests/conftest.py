"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from moto.server import ThreadedMotoServer


@pytest.fixture
def moto_server() -> Iterator[str]:
    """Spin up an in-process moto server for the duration of one test.

    Faster than the Docker-based motoserver image used in the runtime, and
    avoids requiring Docker for unit/integration tests.
    """
    server = ThreadedMotoServer(port=0)  # ephemeral port
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()
