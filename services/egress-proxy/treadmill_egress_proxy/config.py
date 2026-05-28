"""Per-worker allowlist config loading for the egress proxy (ADR-0060)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class WorkerAllowlist(BaseModel):
    """Allowlist config for a single worker IP."""

    model_config = ConfigDict(extra="forbid")

    worker_ip: str
    always_allowed: list[str]
    install_allowed: list[str]
    install_credential_hash: str


class ConfigStore:
    """Loads WorkerAllowlist configs from *.json files in a directory.

    Files are keyed by worker_ip and cached by mtime so re-reads only
    happen when a file changes.
    """

    def __init__(self, config_dir: str | Path) -> None:
        self._config_dir = Path(config_dir)
        # path -> (mtime, WorkerAllowlist)
        self._cache: dict[Path, tuple[float, WorkerAllowlist]] = {}
        # worker_ip -> WorkerAllowlist (fast lookup after load)
        self._by_ip: dict[str, WorkerAllowlist] = {}

    def _reload(self) -> None:
        seen_paths: set[Path] = set()
        for p in self._config_dir.glob("*.json"):
            seen_paths.add(p)
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            cached = self._cache.get(p)
            if cached is not None and cached[0] == mtime:
                continue
            try:
                data = json.loads(p.read_text())
                allowlist = WorkerAllowlist.model_validate(data)
            except Exception:
                continue
            self._cache[p] = (mtime, allowlist)

        # Remove stale entries
        for p in list(self._cache):
            if p not in seen_paths:
                old = self._cache.pop(p)
                self._by_ip.pop(old[1].worker_ip, None)

        # Rebuild ip index
        self._by_ip = {entry.worker_ip: entry for _, entry in self._cache.values()}

    def get(self, worker_ip: str) -> WorkerAllowlist | None:
        self._reload()
        return self._by_ip.get(worker_ip)
