- **[#386] Operator identity ACL + tailnet-only bind + deny-non-operator gate
  (task 5bfd5489, ADR-0097)**: new `services/api/treadmill_api/operator_access/`
  package. `acl.py::is_operator(identity, *, operator_identity) -> bool` —
  the only gate; exact match; empty/None denied.
  `bind.py::tailnet_bind_addr(candidate) -> str` — ALLOWLIST: accepts ONLY the
  Tailscale CGNAT range 100.64.0.0/10 (via `ipaddress.IPv4Network` membership);
  raises ValueError for everything else including LAN (192.168.x.x, 10.x.x.x),
  public IPs, loopback, wildcard, non-IP hostnames, and case-variants like
  "LOCALHOST" (fail-closed / allow-only-tailnet). `funnel_enabled() -> bool` —
  constant False (ADR-0097 §Decision 4). Tests: 19 tests — behavioral + static
  falsifier `test_operator_access_source_is_clean` (wildcard literal + Funnel-True
  scan) with non-vacuous meta-checks for both patterns.
