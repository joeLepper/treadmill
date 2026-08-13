"""Operator identity ACL for the Tailscale terminal bridge (ADR-0097 §Decision 4).

Narrows the tailnet boundary to a single operator identity to shrink blast
radius. No tokens or passwords are issued: Tailscale supplies the caller
identity; this module only compares it against the configured operator identity.
"""

from __future__ import annotations


def is_operator(identity: str | None, *, operator_identity: str) -> bool:
    """Return True iff *identity* exactly equals the configured operator identity.

    Empty, None, or any unknown identity is DENIED (returns False). The
    comparison is exact and case-sensitive — Tailscale identities are treated
    as opaque strings here.
    """
    if not identity or not operator_identity:
        return False
    return identity == operator_identity
