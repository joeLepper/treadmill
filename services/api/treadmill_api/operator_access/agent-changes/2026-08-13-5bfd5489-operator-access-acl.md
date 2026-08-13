- **[#TBD] Operator identity ACL + tailnet-only bind + deny-non-operator gate
  (task 5bfd5489, ADR-0097)**: new `operator_access/` package.
  `acl.py::is_operator(identity, *, operator_identity)` — exact-match identity
  gate; empty/None/unknown identity is denied.
  `bind.py::tailnet_bind_addr(candidate)` — raises ValueError for wildcard,
  loopback, empty, and "localhost"; returns CGNAT addresses unchanged. Uses
  `ipaddress.ip_address().is_unspecified/.is_loopback` — no wildcard literal in
  source (so the static falsifier in `test_operator_access.py` passes).
  `funnel_enabled()` — constant False (ADR-0097 §Decision 4).
  Tests: behavioral allow/deny + static wildcard/Funnel grep gate with
  non-vacuous meta-check.
