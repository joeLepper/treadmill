"""Operator review router package (ADR-0070).

Backs the operator review UI — the labeling interface whose API hooks
consume the endpoints declared here. Per ADR-0070, every endpoint mirrors
a row in a review-queue table built on ``ReviewQueueRowMixin``.

Auto-discovery
--------------

This package exposes ONE aggregator ``router`` mounted at
``/api/v1/review`` and mirrors the ``routers/dashboard/`` and
``routers/triage/`` contract: every sibling ``.py`` module that defines a
module-level ``router = APIRouter()`` is auto-discovered and mounted
underneath. Drop a file with a ``router`` attribute into this directory
and it gets wired without editing this file or ``app.py``.

The factory module (which exposes ``build_review_router()`` rather than a
module-level ``router``) is automatically skipped by the discovery loop —
it only mounts modules where ``getattr(module, "router", None)`` is an
``APIRouter``.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:
    from types import ModuleType

router = APIRouter(prefix="/api/v1/review", tags=["review"])


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
