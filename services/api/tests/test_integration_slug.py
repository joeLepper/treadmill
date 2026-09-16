"""Pure (DB-free) foils for ADR-0118 ``integration_slug`` — the plan-doc → branch-slug derivation
(coordinator template §3.1a). The approve→integration wiring that uses it is covered by the
real-DB foils in ``test_integration_dispatch_consumer.py``; this pins the derivation alone."""

from __future__ import annotations

from treadmill_api.coordination.dispatch_consumer import integration_slug


def test_derives_basename_with_date_prefix_stripping_md():
    assert integration_slug("docs/plans/2026-09-13-fix-auth.md") == "2026-09-13-fix-auth"


def test_basename_only_path():
    assert integration_slug("2026-09-13-x.md") == "2026-09-13-x"


def test_keeps_non_md_basename_verbatim():
    # only a trailing ``.md`` is stripped; an internal dot stays.
    assert integration_slug("docs/plans/2026-09-13-v1.2.md") == "2026-09-13-v1.2"


def test_none_and_empty_return_none():
    assert integration_slug(None) is None
    assert integration_slug("") is None
