"""Heuristic ADR ↔ API-surface coverage check.

Post-mortem surprise B from the 2026-06-09 ADR-0085+0086 plan: the
coordinator prompt referenced ``POST /api/v1/task_prs`` (defined by
ADR-0086) but no such endpoint existed in the API when the plan
dispatched. Alan caught the gap by hand during the brief review; this
module is the structural gate that catches it automatically next time.

Heuristic. v1 ships with regex over obvious HTTP-verb + ``/api/v1/``
patterns. Goal is "ADR referenced an endpoint that nobody implemented"
not exhaustive route reflection — if the regex misses a future
endpoint shape (websockets, GraphQL, etc.) the check stays silent
rather than false-positiving the operator.

Output is WARNINGS, not errors. ADRs legitimately reference future
endpoints as design intent; the gate's job is to surface gaps the
operator should be aware of before briefs go out, not to block plan
dispatch.

Surfaces:

  * :func:`extract_referenced_adrs` — find ADR-XXXX patterns in a
    plan-doc markdown body and resolve them to ``docs/adrs/`` paths.
  * :func:`extract_adr_endpoints` — regex over an ADR's markdown body
    for HTTP-verb + ``/api/v1/`` references.
  * :func:`extract_actual_routes` — walk
    ``services/api/treadmill_api/routers/`` + return the
    ``(method, normalised-path)`` set the API exposes today. Combines
    the ``APIRouter(prefix=...)`` argument with each
    ``@router.<verb>(...)`` decorator's path so the inventory matches
    what FastAPI actually mounts.
  * :func:`diff` — referenced minus actual, with normalisation so
    ``{repo:path}`` matches ``{repo}`` and trailing-slash variants
    collapse.
  * :func:`check_adr_api_coverage` — the orchestrator. Given a
    plan-doc body + a repo root, returns the list of
    :class:`CoverageGap` records (empty list = clean).

The Typer wrapper in ``treadmill_cli/cli.py``'s ``plan validate``
auto-runs this when the plan-doc references any ADR; a standalone
helper at ``tools/check-adr-api-coverage.py`` runs it for CI.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path


# ── Public types ─────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Endpoint:
    """A single HTTP endpoint observed in either an ADR or the live
    router inventory."""

    method: str  # uppercase HTTP verb (POST / GET / PATCH / DELETE / PUT)
    path: str  # full path starting with /api/v1/...


@dataclasses.dataclass(frozen=True)
class CoverageGap:
    """An ADR-referenced endpoint that has no matching route in the API."""

    adr_id: str  # e.g. "ADR-0086"
    adr_path: Path  # the file the reference came from
    endpoint: Endpoint  # the (method, path) that was unmatched


# ── ADR reference extraction ─────────────────────────────────────────────────


_ADR_REF_PATTERN = re.compile(
    r"\bADR-(\d{4})\b",
    re.IGNORECASE,
)


def extract_referenced_adrs(
    plan_doc_text: str,
    *,
    adrs_dir: Path,
) -> list[Path]:
    """Find every ``ADR-XXXX`` reference in the plan doc + resolve to
    its file path under ``docs/adrs/``.

    Returns a deduplicated, sorted list. ADRs whose file isn't on disk
    are dropped silently — the check exists to catch missing API
    surfaces, not missing ADR docs.
    """
    seen: set[str] = set()
    for match in _ADR_REF_PATTERN.finditer(plan_doc_text):
        seen.add(match.group(1))

    paths: list[Path] = []
    for adr_num in sorted(seen):
        # Conventional file shape: docs/adrs/0086-<slug>.md
        candidates = sorted(adrs_dir.glob(f"{adr_num}-*.md"))
        if candidates:
            paths.append(candidates[0])
    return paths


# ── Endpoint extraction (ADR side) ───────────────────────────────────────────


_ADR_ENDPOINT_PATTERN = re.compile(
    # Method preceded by a non-letter (anchor), then whitespace, then
    # /api/v1/... up to the first whitespace, backtick, or
    # punctuation that wouldn't be part of a path-template.
    r"\b(POST|GET|PATCH|DELETE|PUT)\s+(/api/v1/[A-Za-z0-9_./{}:?-]+)",
)


def extract_adr_endpoints(adr_text: str) -> list[Endpoint]:
    """Pull every HTTP-verb + /api/v1/ reference out of an ADR body.

    Strips trailing punctuation the operator may have written
    (commas, closing parens, periods) so ``POST /api/v1/task_prs.``
    still matches the ``task_prs`` route.
    """
    found: list[Endpoint] = []
    for match in _ADR_ENDPOINT_PATTERN.finditer(adr_text):
        method = match.group(1).upper()
        path = match.group(2).rstrip(".,;:)\"'`")
        found.append(Endpoint(method=method, path=path))
    return found


# ── Route inventory (router side) ────────────────────────────────────────────


_ROUTER_PREFIX_PATTERN = re.compile(
    r"APIRouter\s*\(\s*prefix\s*=\s*(['\"])(?P<prefix>[^'\"]*)\1",
)

_ROUTE_DECORATOR_PATTERN = re.compile(
    r"@router\.(?P<method>post|get|patch|delete|put)\s*\(\s*"
    r"(['\"])(?P<path>[^'\"]*)\2",
    re.IGNORECASE,
)


def extract_actual_routes(routers_dir: Path) -> list[Endpoint]:
    """Walk every ``*.py`` under ``routers_dir`` and return the full
    HTTP inventory the API exposes.

    Each router file declares its own prefix on
    ``APIRouter(prefix=...)``; each ``@router.<verb>(...)`` decorator
    appends a suffix. We combine them so the resulting paths match
    what FastAPI mounts via ``app.include_router(router)``.

    Routers without an explicit prefix (e.g. ``APIRouter()`` for a
    no-prefix mount) use the empty string. Decorators with the empty
    string for path (e.g. ``@router.post("")``) collapse to just the
    prefix — i.e. the bare resource root. This matches the live
    behaviour and how ``/api/v1/plans`` is mounted today.
    """
    routes: list[Endpoint] = []
    for py_file in sorted(routers_dir.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        text = py_file.read_text(encoding="utf-8")
        prefix_match = _ROUTER_PREFIX_PATTERN.search(text)
        prefix = prefix_match.group("prefix") if prefix_match else ""
        for route_match in _ROUTE_DECORATOR_PATTERN.finditer(text):
            method = route_match.group("method").upper()
            suffix = route_match.group("path")
            full_path = (prefix + suffix) or "/"
            routes.append(Endpoint(method=method, path=full_path))
    return routes


# ── Diff ─────────────────────────────────────────────────────────────────────


def _normalise_path(path: str) -> str:
    """Collapse ADR / router variants to a comparable shape.

    Three normalisations:

      1. Path-converter syntax: ``{x:path}`` → ``{x}`` so the router
         form and the ADR form match.
      2. Path-parameter NAMES collapse to a wildcard ``{}``. ADRs
         often write ``PATCH /api/v1/workflow_run_steps/{id}`` as
         shorthand for the same endpoint the router exposes as
         ``{step_id}``. Treating both as ``{}`` for the comparison
         eliminates a class of false positives where the API IS
         implemented but the ADR author wrote a different parameter
         name.
      3. Trailing slash collapse (other than the root itself).

    Trade-off accepted: two endpoints whose only difference is which
    path segment is the parameter (``/a/{id}/b`` vs ``/a/c/{id}``)
    would now compare equal at one position but differ at another —
    the canonical form is positional, so this preserves real gaps
    while smoothing the cosmetic ones.
    """
    # 1. Strip path-converter suffix: ``{x:path}`` → ``{x}``
    path = re.sub(r"\{(\w+):[^}]+\}", r"{\1}", path)
    # 2. Collapse parameter NAMES to a wildcard.
    path = re.sub(r"\{\w+\}", "{}", path)
    # 3. Trailing slash collapse.
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


def diff(
    referenced: list[Endpoint],
    actual: list[Endpoint],
) -> list[Endpoint]:
    """Return endpoints in ``referenced`` whose ``(method, path)``
    has no normalised match in ``actual``."""
    actual_set = {(e.method, _normalise_path(e.path)) for e in actual}
    missing: list[Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for ep in referenced:
        key = (ep.method, _normalise_path(ep.path))
        if key in seen:
            continue
        seen.add(key)
        if key not in actual_set:
            missing.append(ep)
    return missing


# ── Top-level orchestrator ───────────────────────────────────────────────────


def check_adr_api_coverage(
    plan_doc_text: str,
    *,
    repo_root: Path,
) -> list[CoverageGap]:
    """Audit a plan doc's ADR references against the live API surface.

    Returns a list of :class:`CoverageGap` records — one per
    referenced-but-unimplemented endpoint. Empty list means clean.

    Plans that reference no ADRs return an empty list (no-op). ADRs
    that exist on disk but reference no HTTP endpoints likewise yield
    no gaps.

    Conventional layout:
      * ADRs at ``docs/adrs/XXXX-<slug>.md``
      * Routers at ``services/api/treadmill_api/routers/*.py``
    """
    adrs_dir = repo_root / "docs" / "adrs"
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"

    if not adrs_dir.exists() or not routers_dir.exists():
        return []

    referenced_adrs = extract_referenced_adrs(
        plan_doc_text, adrs_dir=adrs_dir,
    )
    if not referenced_adrs:
        return []

    actual_routes = extract_actual_routes(routers_dir)

    gaps: list[CoverageGap] = []
    for adr_path in referenced_adrs:
        adr_text = adr_path.read_text(encoding="utf-8")
        referenced_endpoints = extract_adr_endpoints(adr_text)
        if not referenced_endpoints:
            continue
        missing = diff(referenced_endpoints, actual_routes)
        adr_id = _adr_id_from_path(adr_path)
        for endpoint in missing:
            gaps.append(
                CoverageGap(
                    adr_id=adr_id, adr_path=adr_path, endpoint=endpoint,
                )
            )
    return gaps


def _adr_id_from_path(adr_path: Path) -> str:
    """``docs/adrs/0086-coordinator-owns-task-lifecycle.md`` → ``ADR-0086``."""
    stem = adr_path.stem
    head = stem.split("-", 1)[0]
    return f"ADR-{head}"
