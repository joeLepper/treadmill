"""Healthcheck router.

Two endpoints:

  GET /health         — liveness. Always returns 200 if the process is up.
  GET /health/ready   — readiness. Probes each configured dependency
                         (Postgres, Redis); returns 200 if every wired probe
                         is reachable, 503 if any is unreachable.

Probes that are ``not_configured`` (their URL env var is unset) appear in
the response body but do not flip the status code — readiness is about
"the wired deps work," not "every conceivable dep is wired."
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request, Response, status

from treadmill_api.config import Settings, get_settings
from treadmill_api.dependencies import (
    DependencyProbe,
    ProbeStatus,
    overall_status,
    run_probes,
)


router = APIRouter(tags=["health"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe. The process is up if this responds."""
    settings: Settings = get_settings()
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": settings.version,
        "time": _now_iso(),
    }


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    """Readiness probe.

    Reads the list of probes from ``request.app.state.probes`` (set by the
    app lifespan handler). Each probe is run; results are reported in the
    ``checks`` map; overall status reflects whether any probe is unreachable.
    """
    settings: Settings = get_settings()
    probes: list[DependencyProbe] = getattr(request.app.state, "probes", [])

    results = await run_probes(probes)
    overall = overall_status(results)
    if overall is ProbeStatus.UNREACHABLE:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    checks = {r.name: r.to_dict() for r in results}
    return {
        "status": overall.value,
        "service": settings.service_name,
        "version": settings.version,
        "checks": checks,
        "time": _now_iso(),
    }
