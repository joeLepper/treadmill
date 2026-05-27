"""Auto-discovery contract for ``routers/dashboard/`` (ADR-0056, PR-B1).

The dashboard router fans out into many sibling endpoint files
(``overview.py`` in B1, ``task_detail.py``, ``repo_docs.py``,
``actions.py``, ``ws.py`` in B2-B5). To let those PRs ship in parallel
without racing on ``__init__.py``, the aggregator auto-discovers every
sibling module exporting a module-level ``router = APIRouter()``.

These tests pin the contract:

  1. ``overview.router`` is mounted under the aggregator at
     ``/api/v1/dashboard/overview`` without an explicit ``include_router``
     call written by hand in ``__init__.py``.
  2. The discovery mechanism doesn't hard-code module names — the
     ``__init__.py`` source must not reference ``overview`` (or any other
     sibling) by name, so future siblings drop in without edits here.
  3. A freshly authored sibling module gets picked up by the same
     discovery pass: we materialize a temp module, re-run the discovery
     against a fresh aggregator, and assert the new module's router is
     mounted.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi import APIRouter, FastAPI

from treadmill_api.routers import dashboard as dashboard_pkg
from treadmill_api.routers.dashboard import overview as overview_mod


def test_aggregator_router_is_prefixed_and_tagged() -> None:
    """``router`` is the aggregator under ``/api/v1/dashboard``."""
    assert dashboard_pkg.router.prefix == "/api/v1/dashboard"
    assert "dashboard" in dashboard_pkg.router.tags


def test_overview_router_is_auto_discovered() -> None:
    """Discovery picks up ``overview.py`` without an ``__init__.py`` edit."""
    assert "overview" in dashboard_pkg.MOUNTED_MODULES
    # The overview endpoint is reachable through the aggregator's route
    # table. ``router.routes`` is the canonical view of mounted paths.
    paths = {getattr(route, "path", None) for route in dashboard_pkg.router.routes}
    assert "/api/v1/dashboard/overview" in paths


def test_app_mounts_dashboard_router_once() -> None:
    """``app.py`` includes the aggregator exactly once — that's the only
    edit ``app.py`` needs for the entire dashboard endpoint set."""
    from treadmill_api.app import create_app

    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/v1/dashboard/overview" in paths


def test_init_does_not_enumerate_sibling_module_names() -> None:
    """The auto-discovery contract: dropping a new sibling file must NOT
    require editing ``__init__.py``. Source-level assertion guards this
    — the file should not name ``overview`` (or any other sibling).
    """
    init_path = Path(dashboard_pkg.__file__)
    source = init_path.read_text()
    # The literal word ``overview`` (the only sibling that exists today)
    # must not appear in the discovery file. If it does, someone added
    # a name-based mount that defeats the auto-discovery contract.
    assert "overview" not in source, (
        "routers/dashboard/__init__.py mentions 'overview' by name — "
        "auto-discovery should not enumerate siblings, or future PRs "
        "(B2-B5) will conflict on this file"
    )


def test_overview_module_exposes_router_attribute() -> None:
    """Sibling contract: every dashboard module exports a top-level
    ``router``. The aggregator's discovery loop relies on this attribute."""
    assert isinstance(overview_mod.router, APIRouter)


def test_discovery_picks_up_a_freshly_added_sibling(tmp_path: Path) -> None:
    """Re-run the discovery pass against a synthetic sibling and verify
    it gets mounted without any edit to ``__init__.py``. This is the
    forward-compatibility guarantee PRs B2–B5 rely on.
    """
    pkg_dir = Path(dashboard_pkg.__file__).parent
    sibling_path = pkg_dir / "_test_synthetic_sibling.py"
    sibling_path.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/_synthetic-probe')\n"
        "async def _probe() -> dict:\n"
        "    return {'ok': True}\n"
    )
    try:
        # Drop any cached module so the importlib walk reloads cleanly.
        sys.modules.pop(
            "treadmill_api.routers.dashboard._test_synthetic_sibling",
            None,
        )
        fresh_aggregator = APIRouter(
            prefix=dashboard_pkg.router.prefix,
            tags=list(dashboard_pkg.router.tags),
        )
        mounted = dashboard_pkg._discover_and_mount(fresh_aggregator)

        assert "_test_synthetic_sibling" in mounted
        # Mount on a FastAPI app to materialize the full path with prefix.
        app = FastAPI()
        app.include_router(fresh_aggregator)
        paths = {getattr(route, "path", None) for route in app.routes}
        assert "/api/v1/dashboard/_synthetic-probe" in paths
    finally:
        sibling_path.unlink(missing_ok=True)
        sys.modules.pop(
            "treadmill_api.routers.dashboard._test_synthetic_sibling",
            None,
        )
        # Reload the dashboard package so the module cache doesn't keep
        # ``_test_synthetic_sibling`` around for downstream tests.
        importlib.reload(dashboard_pkg)
