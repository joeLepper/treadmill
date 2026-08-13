- **[#386] Operator identity ACL + tailnet-only bind + deny-non-operator gate
  (task 5bfd5489, ADR-0097)**: new `operator_access/` package.
  `acl.py::is_operator(identity, *, operator_identity)` — exact-match identity
  gate; empty/None/unknown identity is denied.
  `bind.py::tailnet_bind_addr(candidate)` — ALLOWLIST: accepts ONLY addresses in
  the Tailscale CGNAT range 100.64.0.0/10 (via `ipaddress.IPv4Network` membership);
  raises ValueError for everything else — LAN, public IPs, loopback, wildcard,
  non-IP hostnames, case-variants (fail-closed / allow-only-tailnet).
  `funnel_enabled()` — constant False (ADR-0097 §Decision 4).
  Tests: 19 tests — behavioral allow/deny + static wildcard/Funnel grep gate with
  non-vacuous meta-checks for both patterns.
