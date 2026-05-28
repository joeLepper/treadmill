"""Unit tests for the ADR-0059 ``WorkerDeps`` + ``BinarySpec`` Pydantic shapes.

Sandbox-safe: no DB, no network — pure import + validation. The shape
constraints are load-bearing (operator-curated input is what the worker
trusts at materialization time), so each validator gets its own
positive + negative cases.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from treadmill_api.models.onboarding import BinarySpec, WorkerDeps


_VALID_BINARY = {
    "name": "cdk",
    "download_url": "https://example.com/cdk",
    "sha256_checksum": "a" * 64,
    "target_path": "/var/treadmill/repo-bin/cdk",
}


# ── WorkerDeps ───────────────────────────────────────────────────────────────


def test_worker_deps_defaults_to_empty_lists() -> None:
    deps = WorkerDeps()
    assert deps.python == []
    assert deps.node == []
    assert deps.binaries == []


def test_worker_deps_round_trips_empty_through_model_dump() -> None:
    deps = WorkerDeps()
    restored = WorkerDeps.model_validate(deps.model_dump())
    assert restored == deps


def test_worker_deps_round_trips_with_lists() -> None:
    deps = WorkerDeps(
        python=["aws-cdk-lib==2.214.0", "constructs==10.3.0"],
        node=["typescript@5.4.5"],
        binaries=[BinarySpec(**_VALID_BINARY)],
    )
    restored = WorkerDeps.model_validate(deps.model_dump())
    assert restored == deps


def test_worker_deps_forbids_unknown_fields() -> None:
    """The ADR drops apt from v1; ``extra='forbid'`` is what enforces it."""
    with pytest.raises(ValidationError):
        WorkerDeps.model_validate({"apt": ["curl"]})


# ── BinarySpec — checksum validation ─────────────────────────────────────────


def test_binary_spec_accepts_lowercase_64_hex_checksum() -> None:
    spec = BinarySpec(**{**_VALID_BINARY, "sha256_checksum": "0" * 64})
    assert spec.sha256_checksum == "0" * 64


def test_binary_spec_rejects_uppercase_checksum() -> None:
    with pytest.raises(ValidationError):
        BinarySpec(**{**_VALID_BINARY, "sha256_checksum": "A" * 64})


def test_binary_spec_rejects_short_checksum() -> None:
    with pytest.raises(ValidationError):
        BinarySpec(**{**_VALID_BINARY, "sha256_checksum": "a" * 63})


def test_binary_spec_rejects_long_checksum() -> None:
    with pytest.raises(ValidationError):
        BinarySpec(**{**_VALID_BINARY, "sha256_checksum": "a" * 65})


def test_binary_spec_rejects_non_hex_checksum() -> None:
    with pytest.raises(ValidationError):
        BinarySpec(**{**_VALID_BINARY, "sha256_checksum": "g" * 64})


# ── BinarySpec — target_path validation ──────────────────────────────────────


def test_binary_spec_accepts_target_path_under_repo_bin_prefix() -> None:
    spec = BinarySpec(
        **{**_VALID_BINARY, "target_path": "/var/treadmill/repo-bin/sub/cdk"}
    )
    assert spec.target_path == "/var/treadmill/repo-bin/sub/cdk"


def test_binary_spec_rejects_target_path_outside_repo_bin_prefix() -> None:
    with pytest.raises(ValidationError):
        BinarySpec(**{**_VALID_BINARY, "target_path": "/tmp/cdk"})


def test_binary_spec_rejects_target_path_with_sibling_prefix() -> None:
    """``/var/treadmill/repo-binary/`` is NOT a valid prefix — the
    materialization spec pins the exact ``/var/treadmill/repo-bin/``."""
    with pytest.raises(ValidationError):
        BinarySpec(
            **{**_VALID_BINARY, "target_path": "/var/treadmill/repo-binary/cdk"}
        )


def test_binary_spec_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        BinarySpec(**{**_VALID_BINARY, "name": ""})


def test_binary_spec_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        BinarySpec(**{**_VALID_BINARY, "extra_attr": "unexpected"})
