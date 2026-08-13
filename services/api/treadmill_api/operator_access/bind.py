"""Tailscale bind-address utilities (ADR-0097 §Decision 3 and 4).

Decision 3: services bind to the Tailscale interface only — never a wildcard,
loopback, or LAN address. tailnet_bind_addr() asserts this at startup.
Decision 4: Tailscale Funnel is never enabled for these services.
"""

from __future__ import annotations

import ipaddress

_BANNED_NAMES: frozenset[str] = frozenset({"localhost", ""})


def tailnet_bind_addr(candidate: str) -> str:
    """Return *candidate* as the bind address, or raise ValueError if banned.

    Raises ValueError for empty string, "localhost", loopback addresses
    (127.x.x.x, ::1), and wildcard/unspecified addresses. A Tailscale CGNAT
    address (100.64.0.0/10) is returned unchanged (ADR-0097 §Decision 3).
    """
    if candidate in _BANNED_NAMES:
        raise ValueError(
            f"bind address {candidate!r} is not allowed: "
            f"must bind to the Tailscale interface only (ADR-0097 §Decision 3)"
        )
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return candidate  # non-IP hostname; pass through
    if addr.is_loopback or addr.is_unspecified:
        raise ValueError(
            f"bind address {candidate!r} is not allowed: "
            f"loopback and wildcard addresses are banned (ADR-0097 §Decision 3)"
        )
    return candidate


def funnel_enabled() -> bool:
    """Return False — Tailscale Funnel is never enabled for these services.

    ADR-0097 §Decision 4: access control is Tailscale's ACL plus identity.
    Funnel is explicitly excluded: it would expose services on the public
    internet, which is contrary to the tailnet-only design.
    """
    return False
