"""Unit tests for the Phase A.3 CDK-app dispatch logic.

Per the Week-4 plan (``docs/plans/2026-05-13-week-4-dev-local-deployment.md``)
the app entrypoint reads two CDK-context flags — ``mode`` and
``deployment_id`` — and dispatches to a stack class (or to a no-op).
:func:`treadmill_infra.app.synthesize` is the factored, testable form of
that dispatch.

The matrix covered here mirrors the plan's Phase A.3 bullet list:

- ``mode=dev_local`` + ``deployment_id=personal`` → one
  ``TreadmillCloudLite`` stack, CFN name ``TreadmillPersonalCloudLite``.
- ``mode=dev_local`` without ``deployment_id`` → raises.
- ``mode=fully_local`` → no stacks.
- ``mode=None`` (unset) → no stacks.
- ``mode=fully_remote`` → no stacks (placeholder for future ADR).
- ``mode=garbage`` → raises with a message naming allowed modes.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest

from treadmill_infra.app import ALLOWED_MODES, synthesize
from treadmill_infra.stacks import TreadmillCloudLite


# ── dev_local: happy path ─────────────────────────────────────────────────────


def test_dev_local_with_deployment_id_synths_cloud_lite():
    app = cdk.App()
    stacks = synthesize(
        app, {"mode": "dev_local", "deployment_id": "personal"},
    )

    assert len(stacks) == 1
    [stack] = stacks
    assert isinstance(stack, TreadmillCloudLite)
    assert stack.deployment_id == "personal"
    # The CFN stack name is derived from deployment_id per ADR-0016.
    assert stack.stack_name == "TreadmillPersonalCloudLite"


def test_dev_local_uses_pascal_case_stack_name_for_arbitrary_id():
    """Sanity check that the dispatch wires deployment_id through
    ``_stack_name_for`` rather than reusing a fixed constant."""
    app = cdk.App()
    stacks = synthesize(
        app, {"mode": "dev_local", "deployment_id": "strongdm"},
    )

    [stack] = stacks
    assert stack.stack_name == "TreadmillStrongdmCloudLite"


# ── dev_local: missing deployment_id ──────────────────────────────────────────


def test_dev_local_without_deployment_id_raises():
    app = cdk.App()
    with pytest.raises(ValueError, match="deployment_id"):
        synthesize(app, {"mode": "dev_local"})


def test_dev_local_with_empty_deployment_id_raises():
    """Empty string is treated as missing — falsy values fail the same
    way ``None`` does so a typo in the context flag fails loud."""
    app = cdk.App()
    with pytest.raises(ValueError, match="deployment_id"):
        synthesize(app, {"mode": "dev_local", "deployment_id": ""})


# ── fully_local / unset: no-op ────────────────────────────────────────────────


def test_fully_local_synths_no_stacks():
    app = cdk.App()
    stacks = synthesize(app, {"mode": "fully_local"})
    assert stacks == []


def test_unset_mode_synths_no_stacks():
    """An empty context dict (``mode is None``) is treated as fully_local
    — ``cdk synth`` with no flags exits cleanly rather than crashing."""
    app = cdk.App()
    stacks = synthesize(app, {})
    assert stacks == []


def test_explicit_none_mode_synths_no_stacks():
    app = cdk.App()
    stacks = synthesize(app, {"mode": None, "deployment_id": None})
    assert stacks == []


# ── fully_remote: no-op placeholder ───────────────────────────────────────────


def test_fully_remote_synths_no_stacks(capsys):
    """fully_remote is reserved for ``TreadmillCloudFull`` (future ADR).
    Until then it no-ops with a printed informational message."""
    app = cdk.App()
    stacks = synthesize(app, {"mode": "fully_remote"})

    assert stacks == []
    captured = capsys.readouterr()
    # The informational message goes to stderr.
    assert "fully_remote" in captured.err
    assert "out of scope" in captured.err.lower() or "future" in captured.err.lower()


# ── unknown modes: hard failure ───────────────────────────────────────────────


def test_unknown_mode_raises_with_allowed_modes_listed():
    app = cdk.App()
    with pytest.raises(ValueError) as exc_info:
        synthesize(app, {"mode": "garbage"})

    msg = str(exc_info.value)
    assert "garbage" in msg
    # The message names each allowed mode so the operator can fix the
    # typo without consulting a separate doc.
    for allowed in ALLOWED_MODES:
        assert allowed in msg


def test_kebab_case_mode_is_rejected():
    """ADR-0016 §"Canonical spellings" commits to snake_case literals
    (``dev_local``). The kebab-case form (``dev-local``) is a prose-only
    spelling and must not work as a CDK context flag — otherwise drift
    sneaks in."""
    app = cdk.App()
    with pytest.raises(ValueError, match="dev-local"):
        synthesize(app, {"mode": "dev-local", "deployment_id": "personal"})


def test_upper_snake_mode_is_rejected():
    """The Python enum *member* is UPPER_SNAKE (``DEV_LOCAL``) but the
    enum *value* (and the CDK-context literal) is lower_snake. UPPER_SNAKE
    in context is therefore a bug and must fail."""
    app = cdk.App()
    with pytest.raises(ValueError, match="DEV_LOCAL"):
        synthesize(app, {"mode": "DEV_LOCAL", "deployment_id": "personal"})


# ── allow-list shape ──────────────────────────────────────────────────────────


def test_allowed_modes_are_exactly_the_three_canonical_spellings():
    """Regression net: the allowed-modes set must match ADR-0016's
    canonical-spellings table exactly. New modes added without
    documenting the spelling break this assertion."""
    assert ALLOWED_MODES == frozenset({"fully_local", "dev_local", "fully_remote"})
