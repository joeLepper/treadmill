"""Entrypoint for treadmill-egress-proxy."""

from __future__ import annotations

import asyncio
import os

from .config import ConfigStore
from .proxy import run_proxy


def main() -> None:
    # Default config dir matches the autoscaler's spawn mount in
    # ``tools/local-adapter/treadmill_local/egress_proxy.py``
    # (``ensure_egress_proxy_container`` mounts the operator-side
    # config dir read-only at ``/etc/egress-proxy-config``).
    config_dir = os.environ.get("EGRESS_PROXY_CONFIG_DIR", "/etc/egress-proxy-config")
    port = int(os.environ.get("EGRESS_PROXY_PORT", "3128"))
    store = ConfigStore(config_dir)
    asyncio.run(run_proxy("0.0.0.0", port, store))


if __name__ == "__main__":
    main()
