"""Client-side onboarding helpers for ``treadmill-local repo onboard``.

Per ADR-0051, bootstrap is operator-initiated from inside the target
repo's working directory: the local CLI infers the repo from the cwd's
git remote, builds a minimal :class:`repo_profile` from the checkout,
and POSTs it to the deployment's onboard endpoint. This module hosts
the pure, testable helpers; the CLI command wires them together.

Decoupled from ``treadmill_api`` on purpose — we hand the server a
plain dict and let the endpoint own the schema + ``recommend_mode``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Map of common file extensions → language label. Order isn't important;
# we sort by count when picking the "top few".
_LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".scala": "scala",
    ".sh": "shell",
}

# Directories we skip when walking the tree for language stats / doc
# discovery. Standard "ignore me" set — vendored deps, VCS metadata,
# build artifacts.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "vendor", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", "target", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".next", ".cache",
})

_MAX_DOC_PATHS = 8
_MAX_COMPONENTS = 16
_MAX_LANGUAGES = 5


def infer_repo(remote_url: str) -> str:
    """Parse ``owner/name`` from a git remote URL.

    Handles both common GitHub remote forms:
      - ``git@github.com:owner/name.git``
      - ``https://github.com/owner/name(.git)``

    The ``.git`` suffix is stripped if present. Raises ``ValueError``
    when the URL doesn't look like a GitHub remote.
    """
    url = remote_url.strip()
    if not url:
        raise ValueError("remote URL is empty")

    # ssh form: git@github.com:owner/name(.git)
    ssh = re.match(r"^git@[^:]+:(?P<path>[^/]+/[^/]+?)(?:\.git)?/?$", url)
    if ssh:
        return ssh.group("path")

    # https form: https://github.com/owner/name(.git)
    https = re.match(
        r"^https?://[^/]+/(?P<path>[^/]+/[^/]+?)(?:\.git)?/?$",
        url,
    )
    if https:
        return https.group("path")

    raise ValueError(f"cannot infer owner/name from remote URL: {remote_url!r}")


def _detect_languages(root: Path) -> list[str]:
    """Return the top languages in *root* by file count."""
    counts: dict[str, int] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip anything under a directory in the skip set.
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        lang = _LANGUAGE_BY_EXT.get(path.suffix.lower())
        if lang is None:
            continue
        counts[lang] = counts.get(lang, 0) + 1
    # Sort by count desc, then language name for determinism.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [lang for lang, _ in ranked[:_MAX_LANGUAGES]]


def _detect_commands(root: Path) -> tuple[str | None, str | None, str | None]:
    """Best-effort build/test/lint commands from common project markers.

    Returns ``(build, test, lint)``. ``None`` whenever we can't make a
    confident guess — the server is the source of truth on what to run
    and would rather have a null than a wrong guess.
    """
    build: str | None = None
    test: str | None = None
    lint: str | None = None

    if (root / "pyproject.toml").exists():
        test = "uv run pytest"
    if (root / "package.json").exists():
        # npm test is the standard; build/lint depend on user's scripts.
        test = test or "npm test"
        build = build or "npm run build"
        lint = lint or "npm run lint"
    if (root / "Makefile").exists():
        build = build or "make"
        test = test or "make test"

    return build, test, lint


def _detect_doc_paths(root: Path) -> list[str]:
    """Return up to :data:`_MAX_DOC_PATHS` doc paths from the checkout."""
    found: list[str] = []
    # READMEs at the root.
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.name.lower().startswith("readme"):
            found.append(entry.name)
    # docs/ tree (one level only — we just want representative paths).
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for entry in sorted(docs_dir.iterdir()):
            if len(found) >= _MAX_DOC_PATHS:
                break
            rel = entry.relative_to(root)
            found.append(str(rel))
    # AGENT.md (root-level) if present.
    if (root / "AGENT.md").exists() and "AGENT.md" not in found:
        found.append("AGENT.md")
    return found[:_MAX_DOC_PATHS]


def _detect_components(root: Path) -> list[str]:
    """Top-level directories that look like components (capped)."""
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        out.append(entry.name)
        if len(out) >= _MAX_COMPONENTS:
            break
    return out


def _has_agent_context(root: Path) -> bool:
    """``True`` when any ``AGENT.md`` is present anywhere in the tree."""
    if (root / "AGENT.md").exists():
        return True
    for path in root.rglob("AGENT.md"):
        rel = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel[:-1]):
            continue
        return True
    return False


def build_profile(root: Path) -> dict[str, Any]:
    """Build a minimal best-effort ``repo_profile`` dict for *root*.

    Keep it simple — ``None``/empty is acceptable for any field. The
    server runs ``recommend_mode`` over the result; rich profiling is
    the server-side ``wf-discover`` productionization (ADR-0050).
    """
    build, test, lint = _detect_commands(root)
    return {
        "languages": _detect_languages(root),
        "build_command": build,
        "test_command": test,
        "lint_command": lint,
        "doc_paths": _detect_doc_paths(root),
        "components": _detect_components(root),
        "ci": "github-actions" if (root / ".github" / "workflows").is_dir() else None,
        "has_agent_context": _has_agent_context(root),
    }


def onboard_payload(
    repo: str,
    profile: dict[str, Any],
    *,
    mode: str | None,
    auto_merge_blocked: bool,
) -> dict[str, Any]:
    """Assemble the onboard request body per ADR-0051's contract.

    The endpoint at ``POST /api/v1/onboarding/repos`` consumes this
    shape. ``mode=None`` defers to the server's ``recommend_mode``.
    """
    return {
        "repo": repo,
        "mode": mode,
        "auto_merge_blocked": auto_merge_blocked,
        "profile": {"repo": repo, **profile},
    }
