"""Tailscale bind-address utilities (ADR-0097 §Decision 3 and 4).

Decision 3: services bind to the Tailscale interface only. tailnet_bind_addr()
is an ALLOWLIST — it accepts ONLY Tailscale CGNAT addresses (100.64.0.0/10)
and raises ValueError for everything else: loopback, wildcard, LAN (192.168.x.x,
10.x.x.x), public IPs, non-IP hostnames. Unevaluable input fails closed.
Decision 4: Tailscale Funnel is never enabled for these services.
"""

from __future__ import annotations

import ipaddress

_TAILSCALE_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")


def tailnet_bind_addr(candidate: str) -> str:
    """Return *candidate* only when it is a Tailscale CGNAT address (100.64.0.0/10).

    ALLOWLIST — raises ValueError for everything outside the CGNAT range:
    - Wildcard/unspecified and loopback addresses
    - LAN addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
    - Public IP addresses
    - Non-IP hostnames and unparseable input (fail-closed, no passthrough)
    - IPv6 (not in scope for this implementation)

    These sessions run with permissions bypassed; a LAN bind would expose a
    permissions-bypassed shell to the entire LAN (ADR-0097 Consequences).
    """
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        raise ValueError(
            f"bind address {candidate!r} is not a valid IP address; "
            f"must be in the Tailscale CGNAT range 100.64.0.0/10 "
            f"(ADR-0097 §Decision 3)"
        ) from None
    if isinstance(addr, ipaddress.IPv4Address) and addr in _TAILSCALE_CGNAT:
        return candidate
    raise ValueError(
        f"bind address {candidate!r} is not in the Tailscale CGNAT range "
        f"(100.64.0.0/10); must bind to the Tailscale interface only "
        f"(ADR-0097 §Decision 3)"
    )


def funnel_enabled() -> bool:
    """Return False — Tailscale Funnel is never enabled for these services.

    ADR-0097 §Decision 4: access control is Tailscale's ACL plus identity.
    Funnel is explicitly excluded: it would expose services on the public
    internet, which is contrary to the tailnet-only design.
    """
    return False
