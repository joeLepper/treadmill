"""Discovery repo-profile schema + conform/adapt recommendation.

Per ADR-0050, ``wf-discover`` (``role-cartographer``, read-only) produces a
structured **repo profile** describing the languages, build/test/lint
commands, doc locations, CI, component layout, and whether ``AGENT``-style
context already exists in the repo. This module holds that schema (decision
1) and the mode recommendation (decision 2).

Kept deliberately independent: no DB model, no router, no import from
``repo_config.py``. The mode is a plain string — ``"conform"`` or
``"adapt"`` — so this module can be merged before the persistence layer
lands.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class RepoProfile:
    """Structured output of discovery for one repo (ADR-0050 decision 1)."""

    repo: str
    languages: list[str] = field(default_factory=list)
    build_command: str | None = None
    test_command: str | None = None
    lint_command: str | None = None
    doc_paths: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    ci: str | None = None
    has_agent_context: bool = False


def to_dict(profile: RepoProfile) -> dict:
    """Serialize a profile to a plain dict (for JSONB persistence later)."""
    return asdict(profile)


def from_dict(data: dict) -> RepoProfile:
    """Build a profile from a plain dict; round-trips with :func:`to_dict`."""
    return RepoProfile(
        repo=data["repo"],
        languages=list(data.get("languages", [])),
        build_command=data.get("build_command"),
        test_command=data.get("test_command"),
        lint_command=data.get("lint_command"),
        doc_paths=list(data.get("doc_paths", [])),
        components=list(data.get("components", [])),
        ci=data.get("ci"),
        has_agent_context=bool(data.get("has_agent_context", False)),
    )


def recommend_mode(profile: RepoProfile) -> str:
    """Recommend ``"conform"`` or ``"adapt"`` per ADR-0050 decision 2.

    A repo that already carries its own discipline gets ``"adapt"`` — its
    operating context stays pristine and the discovered command set drives
    validation. The heuristic: ``has_agent_context`` is True (the repo
    already has ``AGENT``-style context) OR ``len(doc_paths) >= 3`` (the
    repo carries enough internal documentation to lean on). Otherwise the
    repo is sparse and ``"conform"`` is recommended — Treadmill opens PRs
    to seed its context in-tree.

    Discovery only *recommends*; the operator confirms the final choice,
    which then persists as source of truth (the recommendation is a
    starting point, not a verdict).
    """
    if profile.has_agent_context or len(profile.doc_paths) >= 3:
        return "adapt"
    return "conform"
