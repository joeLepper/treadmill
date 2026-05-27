"""Operator dashboard router package (ADR-0056, PR-B1 onward).

This package backs ``services/dashboard/`` — the static React SPA whose
``src/api/queries.ts`` hooks consume aggregation endpoints from here.
Per ADR-0056, ``src/api/types.ts`` is the contract; every endpoint here
matches one of those query shapes field-for-field.

Auto-discovery
--------------

This package exposes ONE aggregator ``router`` mounted at
``/api/v1/dashboard``. Every sibling ``.py`` module that defines a
module-level ``router = APIRouter()`` is auto-discovered and mounted
underneath. Drop a file with a ``router`` attribute into this directory
and it gets wired without editing this file or ``app.py``.

The plan (`docs/plans/2026-05-26-treadmill-dashboard-v1.md` §"PR B task
breakdown") fans the remaining dashboard endpoints out across siblings
(``task_detail.py``, ``repo_docs.py``, ``actions.py``, ``ws.py``) so the
parallel PRs touch disjoint files. Auto-discovery is what keeps those
PRs from racing on ``__init__.py``.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:
    from types import ModuleType

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


def _discover_and_mount(aggregator: APIRouter) -> list[str]:
    """Walk this package and ``include_router`` every sibling module's
    ``router`` attribute.

    Returns the list of mounted module names so tests + logs can assert
    the discovery happened without re-reading the filesystem.
    """
    mounted: list[str] = []
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg:
            continue
        module: ModuleType = importlib.import_module(
            f"{__name__}.{module_info.name}"
        )
        sibling_router = getattr(module, "router", None)
        if isinstance(sibling_router, APIRouter):
            aggregator.include_router(sibling_router)
            mounted.append(module_info.name)
    return mounted


MOUNTED_MODULES: list[str] = _discover_and_mount(router)


__all__ = ["router", "MOUNTED_MODULES"]
