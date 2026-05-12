"""CLI configuration — loaded from environment, with sane local defaults.

The CLI talks to the Treadmill API over HTTP. Two values matter:

  * ``TREADMILL_API_URL``  — base URL of the API. Defaults to
    ``http://localhost:8088`` to match the local-adapter's host port mapping
    for the API service per ADR-0010.
  * ``TREADMILL_API_KEY``  — bearer token (unused at v0; reserved for when
    auth lands per ADR-0009's bootstrap order).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_API_URL = "http://localhost:8088"


@dataclass
class CliConfig:
    api_url: str
    api_key: str | None = None


def load_config() -> CliConfig:
    return CliConfig(
        api_url=os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL).rstrip("/"),
        api_key=os.environ.get("TREADMILL_API_KEY") or None,
    )
