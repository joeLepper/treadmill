"""ADR-0096 drift detection.

Pure module: no network, no process introspection at runtime.
Callers supply paths explicitly so tests can pass temporary trees.

The poller that sources a real release version label from a release artefact
is deferred (out of scope for this task).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Manifest:
    files: dict[str, str]
    release_hash: str
    version: str | None = None


@dataclass(frozen=True)
class DriftReport:
    in_drift: bool
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _combined_hash(file_hashes: dict[str, str]) -> str:
    h = hashlib.sha256()
    for key in sorted(file_hashes):
        h.update(key.encode())
        h.update(file_hashes[key].encode())
    return h.hexdigest()


def manifest_for(paths: list[Path], version: str | None = None) -> Manifest:
    """Build a manifest for a set of runtime files.

    Keys in the files dict are str(path) values, kept consistent with the
    paths argument so callers can pass either absolute or relative paths.
    """
    file_hashes = {str(p): _sha256_file(p) for p in paths}
    return Manifest(
        files=file_hashes,
        release_hash=_combined_hash(file_hashes),
        version=version,
    )


def detect_drift(claimed_manifest: Manifest, live_paths: list[Path]) -> DriftReport:
    """Compare a claimed manifest against the current state of live_paths.

    Returns a DriftReport describing any divergence between what the manifest
    asserts and what is actually on disk.
    """
    live = manifest_for(live_paths, version=claimed_manifest.version)
    claimed_files = claimed_manifest.files
    live_files = live.files

    claimed_keys = set(claimed_files)
    live_keys = set(live_files)

    missing = sorted(claimed_keys - live_keys)
    added = sorted(live_keys - claimed_keys)
    changed = sorted(
        k for k in claimed_keys & live_keys if claimed_files[k] != live_files[k]
    )

    in_drift = bool(missing or added or changed)
    return DriftReport(in_drift=in_drift, changed=changed, missing=missing, added=added)


def same_version_different_code(manifest_a: Manifest, manifest_b: Manifest) -> bool:
    """True when two manifests share a version label but have different release hashes.

    This is the two-hosts-diverged falsifier from ADR-0096: hosts claiming the
    same release should have identical release hashes.  A mismatch means one
    host is not running what it claims.
    """
    if manifest_a.version is None or manifest_b.version is None:
        return False
    return (
        manifest_a.version == manifest_b.version
        and manifest_a.release_hash != manifest_b.release_hash
    )
