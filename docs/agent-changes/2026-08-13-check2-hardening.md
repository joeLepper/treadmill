# 2026-08-13 — Check-2 hardening: strip docstrings before trigger/action match

PR: [#382](https://github.com/joeLepper/treadmill/pull/382) — task e614436c (evaluator rework of #379)

Hardens Check 2 in `services/api/tests/test_no_auto_host_adoption.py` to avoid
false positives on functions with disclaiming docstrings.

**Root cause of the false positive:** `host_registry_store.py::bind_label` carries
the docstring "no caller may rebind automatically on host-unreachable". The original
Check 2 matched trigger+action words over the raw function text including docstrings,
so it flagged `bind_label` as a violation — correct documentation, not forbidden code.

**Fix:** `_executable_func_text()` strips the docstring (first statement when it is
a bare `ast.Constant` string) and comment-only lines before matching. Inline trailing
comments ride their code lines and are kept.

**New additions:**
- `APPROVED_CO_OCCURRENCE_EXCEPTIONS` dict (empty; documented structure for future exemptions)
- `test_check2_flags_real_relocation` — positive meta-test: executable relocation code MUST be flagged
- `test_check2_does_not_flag_disclaiming_docstring` — negative meta-test: the `bind_label` docstring shape MUST NOT be flagged
