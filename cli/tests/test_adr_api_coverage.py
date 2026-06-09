"""Unit tests for the ADR ↔ API-surface coverage check.

Sets up a synthetic ``docs/adrs/`` + ``services/api/treadmill_api/
routers/`` tree under ``tmp_path`` so the tests don't fight the
treadmill repo's live ADR set. Asserts the gap-detection canonical
shape: an ADR that references an unimplemented endpoint produces
exactly one CoverageGap; an ADR whose endpoints are all backed
produces none; plans without ADR refs no-op.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from treadmill_cli.adr_api_coverage import (
    CoverageGap,
    Endpoint,
    check_adr_api_coverage,
    diff,
    extract_actual_routes,
    extract_adr_endpoints,
    extract_referenced_adrs,
)


# ── Fixture helpers ──────────────────────────────────────────────────────────


def _write_adr(adrs_dir: Path, adr_id: str, slug: str, body: str) -> Path:
    """Write a synthetic ADR file. Returns the path."""
    adrs_dir.mkdir(parents=True, exist_ok=True)
    path = adrs_dir / f"{adr_id}-{slug}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _write_router(routers_dir: Path, name: str, body: str) -> Path:
    routers_dir.mkdir(parents=True, exist_ok=True)
    path = routers_dir / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A synthetic repo root with the ``docs/adrs/`` +
    ``services/api/treadmill_api/routers/`` skeleton."""
    (tmp_path / "docs" / "adrs").mkdir(parents=True)
    (tmp_path / "services" / "api" / "treadmill_api" / "routers").mkdir(parents=True)
    return tmp_path


# ── ADR reference extraction ─────────────────────────────────────────────────


def test_extract_referenced_adrs_finds_one_ref(repo_root: Path):
    adrs_dir = repo_root / "docs" / "adrs"
    _write_adr(adrs_dir, "0086", "coordinator", "body")
    plan = "Plan related to ADR-0086 lifecycle."
    refs = extract_referenced_adrs(plan, adrs_dir=adrs_dir)
    assert len(refs) == 1
    assert refs[0].name == "0086-coordinator.md"


def test_extract_referenced_adrs_deduplicates_repeats(repo_root: Path):
    adrs_dir = repo_root / "docs" / "adrs"
    _write_adr(adrs_dir, "0086", "x", "")
    plan = "ADR-0086 mentioned. ADR-0086 again. and ADR-0086 once more."
    assert len(extract_referenced_adrs(plan, adrs_dir=adrs_dir)) == 1


def test_extract_referenced_adrs_ignores_missing_files(repo_root: Path):
    """A plan can reference an ADR that doesn't exist yet (design
    intent). The check skips it silently — it's not what we're
    auditing for."""
    adrs_dir = repo_root / "docs" / "adrs"
    plan = "Plan references ADR-9999 (not yet authored)."
    assert extract_referenced_adrs(plan, adrs_dir=adrs_dir) == []


def test_extract_referenced_adrs_sorts_results(repo_root: Path):
    adrs_dir = repo_root / "docs" / "adrs"
    _write_adr(adrs_dir, "0086", "a", "")
    _write_adr(adrs_dir, "0085", "b", "")
    plan = "Plan related to ADR-0086 and ADR-0085."
    refs = extract_referenced_adrs(plan, adrs_dir=adrs_dir)
    assert [p.name for p in refs] == ["0085-b.md", "0086-a.md"]


def test_extract_referenced_adrs_no_refs_yields_empty():
    assert extract_referenced_adrs("body with no ADRs", adrs_dir=Path("/nope")) == []


# ── ADR endpoint extraction ──────────────────────────────────────────────────


def test_extract_adr_endpoints_finds_post():
    body = "The coordinator calls POST /api/v1/task_prs to register a PR."
    assert extract_adr_endpoints(body) == [
        Endpoint(method="POST", path="/api/v1/task_prs"),
    ]


def test_extract_adr_endpoints_strips_trailing_punctuation():
    body = "Calls POST /api/v1/task_prs."
    assert extract_adr_endpoints(body)[0].path == "/api/v1/task_prs"


def test_extract_adr_endpoints_finds_all_verbs():
    body = (
        "POST /api/v1/a\n"
        "GET /api/v1/b\n"
        "PATCH /api/v1/c/{id}\n"
        "DELETE /api/v1/d\n"
        "PUT /api/v1/e\n"
    )
    methods = sorted(ep.method for ep in extract_adr_endpoints(body))
    assert methods == ["DELETE", "GET", "PATCH", "POST", "PUT"]


def test_extract_adr_endpoints_handles_path_templates():
    body = "PATCH /api/v1/workflow_run_steps/{step_id} updates status."
    out = extract_adr_endpoints(body)
    assert out == [Endpoint(method="PATCH", path="/api/v1/workflow_run_steps/{step_id}")]


def test_extract_adr_endpoints_ignores_non_api_paths():
    body = "Frontend at /frontend or /api/v0/old or POST /some/other/path"
    assert extract_adr_endpoints(body) == []


def test_extract_adr_endpoints_empty_body_yields_empty():
    assert extract_adr_endpoints("") == []


# ── Route inventory ──────────────────────────────────────────────────────────


def test_extract_actual_routes_combines_prefix_and_suffix(repo_root: Path):
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"
    _write_router(routers_dir, "plans", """
from fastapi import APIRouter
router = APIRouter(prefix="/api/v1/plans", tags=["plans"])

@router.post("", response_model=None)
async def create_plan(): ...

@router.get("/{plan_id}", response_model=None)
async def get_plan(): ...
""")
    routes = extract_actual_routes(routers_dir)
    assert set((r.method, r.path) for r in routes) == {
        ("POST", "/api/v1/plans"),
        ("GET", "/api/v1/plans/{plan_id}"),
    }


def test_extract_actual_routes_handles_router_with_no_prefix(repo_root: Path):
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"
    _write_router(routers_dir, "health", """
from fastapi import APIRouter
router = APIRouter()
@router.get("/health")
async def health(): ...
""")
    routes = extract_actual_routes(routers_dir)
    assert routes == [Endpoint(method="GET", path="/health")]


def test_extract_actual_routes_normalises_path_converter_syntax(repo_root: Path):
    """``{repo:path}`` and ``{repo}`` should both match on the diff
    side. extract_actual_routes preserves the raw form; diff
    normalises."""
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"
    _write_router(routers_dir, "team_configs", """
from fastapi import APIRouter
router = APIRouter(prefix="/api/v1")

@router.get("/team_configs/{repo:path}", response_model=None)
async def get_team_config(): ...
""")
    routes = extract_actual_routes(routers_dir)
    assert routes == [
        Endpoint(method="GET", path="/api/v1/team_configs/{repo:path}"),
    ]


def test_extract_actual_routes_walks_multiple_files(repo_root: Path):
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"
    _write_router(routers_dir, "a", "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/v1/a')\n@router.post('')\nasync def x(): ...")
    _write_router(routers_dir, "b", "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/v1/b')\n@router.get('')\nasync def y(): ...")
    routes = extract_actual_routes(routers_dir)
    assert set((r.method, r.path) for r in routes) == {
        ("POST", "/api/v1/a"),
        ("GET", "/api/v1/b"),
    }


def test_extract_actual_routes_skips_dunder_files(repo_root: Path):
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"
    _write_router(routers_dir, "__init__", "# package marker")
    _write_router(routers_dir, "real", "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/v1/real')\n@router.post('')\nasync def x(): ...")
    routes = extract_actual_routes(routers_dir)
    assert routes == [Endpoint(method="POST", path="/api/v1/real")]


# ── Diff ─────────────────────────────────────────────────────────────────────


def test_diff_returns_referenced_minus_actual():
    referenced = [
        Endpoint("POST", "/api/v1/task_prs"),
        Endpoint("GET", "/api/v1/plans"),
    ]
    actual = [
        Endpoint("GET", "/api/v1/plans"),
        Endpoint("POST", "/api/v1/something_else"),
    ]
    assert diff(referenced, actual) == [Endpoint("POST", "/api/v1/task_prs")]


def test_diff_normalises_path_converter_syntax():
    """``{repo}`` in an ADR matches ``{repo:path}`` in a router."""
    referenced = [Endpoint("GET", "/api/v1/team_configs/{repo}")]
    actual = [Endpoint("GET", "/api/v1/team_configs/{repo:path}")]
    assert diff(referenced, actual) == []


def test_diff_normalises_path_parameter_names():
    """ADR-0086 wrote ``PATCH /api/v1/workflow_run_steps/{id}`` as a
    generalisation; the router exposes the same endpoint as
    ``{step_id}``. The check must not flag this as a gap — the API
    IS implemented, the ADR author just used a different parameter
    name."""
    referenced = [Endpoint("PATCH", "/api/v1/workflow_run_steps/{id}")]
    actual = [Endpoint("PATCH", "/api/v1/workflow_run_steps/{step_id}")]
    assert diff(referenced, actual) == []


def test_diff_param_name_normalisation_preserves_positional_gaps():
    """Position-of-the-parameter still matters. ``/a/{id}/b`` and
    ``/a/c/{id}`` are NOT the same endpoint; the parameter is in
    different segments."""
    referenced = [Endpoint("GET", "/api/v1/a/{id}/b")]
    actual = [Endpoint("GET", "/api/v1/a/c/{id}")]
    gaps = diff(referenced, actual)
    assert len(gaps) == 1


def test_diff_normalises_trailing_slash():
    referenced = [Endpoint("POST", "/api/v1/plans/")]
    actual = [Endpoint("POST", "/api/v1/plans")]
    assert diff(referenced, actual) == []


def test_diff_deduplicates_repeated_references():
    referenced = [
        Endpoint("POST", "/api/v1/task_prs"),
        Endpoint("POST", "/api/v1/task_prs"),  # twice in the ADR body
    ]
    actual: list[Endpoint] = []
    gaps = diff(referenced, actual)
    assert len(gaps) == 1


def test_diff_method_mismatch_is_a_gap():
    """An ADR that says POST but the API only has GET is still a gap."""
    referenced = [Endpoint("POST", "/api/v1/plans/{plan_id}")]
    actual = [Endpoint("GET", "/api/v1/plans/{plan_id}")]
    assert diff(referenced, actual) == referenced


# ── Top-level orchestrator ───────────────────────────────────────────────────


def test_check_adr_api_coverage_canonical_gap_case(repo_root: Path):
    """The exact scenario from post-mortem surprise B: ADR-0086 said
    POST /api/v1/task_prs but the route was missing."""
    adrs_dir = repo_root / "docs" / "adrs"
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"

    _write_adr(adrs_dir, "0086", "coordinator", (
        "# ADR-0086\n\n"
        "The coordinator calls POST /api/v1/task_prs to register a PR.\n"
    ))
    _write_router(routers_dir, "plans", (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/v1/plans')\n"
        "@router.post('')\n"
        "async def x(): ...\n"
    ))

    plan_doc = "# Plan\n\nRelated ADR: ADR-0086\n"
    gaps = check_adr_api_coverage(plan_doc, repo_root=repo_root)
    assert len(gaps) == 1
    assert gaps[0].adr_id == "ADR-0086"
    assert gaps[0].endpoint == Endpoint("POST", "/api/v1/task_prs")


def test_check_adr_api_coverage_no_adr_refs_no_ops(repo_root: Path):
    plan_doc = "# Plan\n\nNo ADRs referenced.\n"
    assert check_adr_api_coverage(plan_doc, repo_root=repo_root) == []


def test_check_adr_api_coverage_clean_when_all_endpoints_backed(repo_root: Path):
    adrs_dir = repo_root / "docs" / "adrs"
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"

    _write_adr(adrs_dir, "0086", "ok", (
        "ADR-0086 uses POST /api/v1/task_prs and GET /api/v1/plans/{plan_id}.\n"
    ))
    _write_router(routers_dir, "task_prs", (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/v1')\n"
        "@router.post('/task_prs')\n"
        "async def x(): ...\n"
    ))
    _write_router(routers_dir, "plans", (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/v1/plans')\n"
        "@router.get('/{plan_id}')\n"
        "async def y(): ...\n"
    ))

    plan_doc = "# Plan\n\nRelated: ADR-0086.\n"
    assert check_adr_api_coverage(plan_doc, repo_root=repo_root) == []


def test_check_adr_api_coverage_missing_adrs_dir_returns_empty(tmp_path: Path):
    plan_doc = "ADR-0086 referenced."
    # No docs/adrs/ directory at all.
    assert check_adr_api_coverage(plan_doc, repo_root=tmp_path) == []


def test_check_adr_api_coverage_aggregates_across_multiple_adrs(
    repo_root: Path,
):
    adrs_dir = repo_root / "docs" / "adrs"
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"

    _write_adr(adrs_dir, "0085", "a", "Uses POST /api/v1/missing_one.\n")
    _write_adr(adrs_dir, "0086", "b", "Uses POST /api/v1/missing_two.\n")
    _write_router(routers_dir, "stub", (
        "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/v1')\n"
        "@router.get('/exists')\nasync def x(): ...\n"
    ))

    plan_doc = "Plan referencing ADR-0085 and ADR-0086.\n"
    gaps = check_adr_api_coverage(plan_doc, repo_root=repo_root)
    assert len(gaps) == 2
    adr_ids = {g.adr_id for g in gaps}
    assert adr_ids == {"ADR-0085", "ADR-0086"}


def test_check_adr_api_coverage_returns_dataclass_records(repo_root: Path):
    adrs_dir = repo_root / "docs" / "adrs"
    routers_dir = repo_root / "services" / "api" / "treadmill_api" / "routers"
    adr_path = _write_adr(adrs_dir, "0086", "c", "Uses POST /api/v1/missing.\n")
    _write_router(routers_dir, "stub", (
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/h')\n"
        "async def x(): ...\n"
    ))

    gaps = check_adr_api_coverage("Plan ADR-0086.", repo_root=repo_root)
    assert len(gaps) == 1
    gap = gaps[0]
    assert isinstance(gap, CoverageGap)
    assert gap.adr_id == "ADR-0086"
    assert gap.adr_path == adr_path
    assert gap.endpoint.method == "POST"
    assert gap.endpoint.path == "/api/v1/missing"
