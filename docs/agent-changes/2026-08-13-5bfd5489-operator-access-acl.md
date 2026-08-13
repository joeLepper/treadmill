- **[#TBD] Operator identity ACL + tailnet-only bind + deny-non-operator gate
  (task 5bfd5489, ADR-0097)**: new `services/api/treadmill_api/operator_access/`
  package. `acl.py::is_operator(identity, *, operator_identity) -> bool` —
  the only gate; exact match; empty/None denied. `bind.py::tailnet_bind_addr(candidate)
  -> str` — raises ValueError for wildcard (detected via `ipaddress.is_unspecified`),
  loopback (`is_loopback`), "", "localhost"; returns CGNAT addresses unchanged.
  `funnel_enabled() -> bool` — constant False (ADR-0097 §Decision 4).
  Includes a static falsifier `test_operator_access_source_is_clean` that scans
  operator_access/*.py for banned patterns (wildcard literal, Funnel-True) with
  a non-vacuous meta-check `test_wildcard_scan_detects_violation`.
