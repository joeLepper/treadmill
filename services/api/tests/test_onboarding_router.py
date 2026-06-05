"""Unit tests for the onboarding router (ADR-0051).

Builds a minimal FastAPI app with only the onboarding router and overrides
``get_session`` with an in-memory stub — no live DB, no engine. The
``OnboardingStore`` is monkeypatched on the router module so a fake records
the upserts that would have been issued.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.onboarding import BinarySpec, WorkerDeps
from treadmill_api.repo_config import RepoConfig
from treadmill_api.repo_profile import RepoProfile
from treadmill_api.routers import onboarding as onboarding_router_mod


class _StubSession:
    """Minimal async-session stub.

    The handler never touches add/execute on this session — the upserts go
    through ``OnboardingStore`` which we replace with a fake. Only
    ``commit`` is exercised here, and we record it for assertions.
    """

    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class _FakeStore:
    """Records the profile + config the handler would have upserted."""

    def __init__(self) -> None:
        self.profiles: list[RepoProfile] = []
        self.configs: list[RepoConfig] = []
        # Seeds the GET handler's lookup; defaults to None (404).
        self.config_by_repo: dict[str, RepoConfig] = {}

    async def upsert_repo_profile(
        self, session: Any, profile: RepoProfile
    ) -> None:
        self.profiles.append(profile)

    async def upsert_repo_config(
        self, session: Any, config: RepoConfig
    ) -> None:
        self.configs.append(config)

    async def get_repo_config(
        self, session: Any, repo: str
    ) -> RepoConfig | None:
        return self.config_by_repo.get(repo)


def _build_app(
    session: _StubSession, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    app = FastAPI()
    app.include_router(onboarding_router_mod.router)

    def _session_override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    monkeypatch.setattr(
        onboarding_router_mod, "OnboardingStore", lambda: store
    )
    return app


# ── happy path: explicit mode ────────────────────────────────────────────────


def test_onboard_repo_explicit_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/example",
        "mode": "adapt",
        "auto_merge_blocked": True,
        "profile": {
            "languages": ["python", "typescript"],
            "build_command": "make build",
            "test_command": "pytest",
            "lint_command": "ruff check .",
            "doc_paths": ["README.md"],
            "components": ["api", "worker"],
            "ci": "github-actions",
            "has_agent_context": False,
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "repo": "owner/example",
        "mode": "adapt",
        "auto_merge_blocked": True,
        "claude_account": None,
        "worker_deps": {"python": [], "node": [], "binaries": []},
    }
    assert session.committed
    assert len(store.profiles) == 1
    assert len(store.configs) == 1
    profile = store.profiles[0]
    assert profile.repo == "owner/example"
    assert profile.test_command == "pytest"
    assert profile.lint_command == "ruff check ."
    assert profile.languages == ["python", "typescript"]
    config = store.configs[0]
    assert config.repo == "owner/example"
    assert config.mode == "adapt"
    assert config.auto_merge_blocked is True
    assert config.test_command == "pytest"
    assert config.lint_command == "ruff check ."


# ── happy path: mode omitted → recommend_mode ────────────────────────────────


def test_onboard_repo_recommends_conform_when_mode_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sparse repo (no agent context, only one doc) gets ``conform``."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/sparse",
        "mode": None,
        "profile": {
            "languages": ["python"],
            "doc_paths": ["README.md"],
            "has_agent_context": False,
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["repo"] == "owner/sparse"
    assert payload["mode"] == "conform"
    assert payload["auto_merge_blocked"] is False
    assert store.configs[0].mode == "conform"


def test_onboard_repo_recommends_adapt_when_repo_has_agent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo with agent context already in-tree gets ``adapt``."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/seasoned",
        "profile": {
            "languages": ["go"],
            "doc_paths": ["AGENT.md"],
            "has_agent_context": True,
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "adapt"
    assert store.configs[0].mode == "adapt"


# ── profile["repo"] defaulting ───────────────────────────────────────────────


def test_onboard_repo_defaults_profile_repo_from_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile dict need not carry its own ``repo`` — it inherits from
    ``body.repo`` so callers don't have to repeat the identifier."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/inherits",
        "profile": {"languages": ["python"]},
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    assert store.profiles[0].repo == "owner/inherits"


# ── invalid mode rejection ───────────────────────────────────────────────────


def test_onboard_repo_rejects_invalid_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/example",
        "mode": "bogus",
        "profile": {"languages": ["python"]},
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 400, response.text
    assert "mode" in response.json()["detail"]
    assert not session.committed
    assert store.profiles == []
    assert store.configs == []


# ── GET /repos/{repo} — mode lookup for the authoring skill ───────────────────


def test_get_repo_returns_config(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _StubSession()
    store = _FakeStore()
    store.config_by_repo["owner/example"] = RepoConfig(
        repo="owner/example",
        mode="adapt",
        auto_merge_blocked=True,
        test_command="make unit_test",
        lint_command=None,
    )
    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/onboarding/repos/owner/example")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "repo": "owner/example",
        "mode": "adapt",
        "auto_merge_blocked": True,
        "test_command": "make unit_test",
        "lint_command": None,
        "claude_account": None,
        "worker_deps": {"python": [], "node": [], "binaries": []},
    }


def test_onboard_repo_accepts_claude_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0055: POST forwards ``claude_account`` to the persisted RepoConfig."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/example",
        "claude_account": "secondary",
        "profile": {"languages": ["python"]},
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["claude_account"] == "secondary"
    assert store.configs[0].claude_account == "secondary"


def test_get_repo_returns_claude_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()
    store.config_by_repo["owner/example"] = RepoConfig(
        repo="owner/example",
        mode="conform",
        claude_account="secondary",
    )
    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/onboarding/repos/owner/example")

    assert response.status_code == 200, response.text
    assert response.json()["claude_account"] == "secondary"


def test_get_repo_404_when_not_onboarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()  # empty config_by_repo
    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/onboarding/repos/owner/missing")

    assert response.status_code == 404, response.text
    assert "not onboarded" in response.json()["detail"]


# ── ADR-0059: worker_deps round-trip ─────────────────────────────────────────


def test_onboard_repo_accepts_worker_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST forwards ``worker_deps`` (Python + Node + binaries) to the
    persisted :class:`RepoConfig` and echoes it on the response."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/example",
        "profile": {"languages": ["python"]},
        "worker_deps": {
            "python": ["aws-cdk-lib==2.214.0"],
            "node": ["typescript@5.4.5"],
            "binaries": [
                {
                    "name": "cdk",
                    "download_url": "https://example.com/cdk",
                    "sha256_checksum": "a" * 64,
                    "target_path": "/var/treadmill/repo-bin/cdk",
                },
            ],
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["worker_deps"] == body["worker_deps"]
    assert store.configs[0].worker_deps == WorkerDeps(
        python=["aws-cdk-lib==2.214.0"],
        node=["typescript@5.4.5"],
        binaries=[
            BinarySpec(
                name="cdk",
                download_url="https://example.com/cdk",
                sha256_checksum="a" * 64,
                target_path="/var/treadmill/repo-bin/cdk",
            )
        ],
    )


def test_onboard_repo_defaults_worker_deps_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``worker_deps`` is omitted the handler hands an empty
    :class:`WorkerDeps` to the store — never ``None`` — so downstream
    callers can lean on the materialized shape."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/example",
        "profile": {"languages": ["python"]},
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["worker_deps"] == {
        "python": [],
        "node": [],
        "binaries": [],
    }
    assert store.configs[0].worker_deps == WorkerDeps()


def test_onboard_repo_rejects_invalid_worker_deps_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pydantic validation rejects uppercase hex in ``sha256_checksum``
    (422); nothing gets persisted."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "owner/example",
        "profile": {"languages": ["python"]},
        "worker_deps": {
            "binaries": [
                {
                    "name": "cdk",
                    "download_url": "https://example.com/cdk",
                    "sha256_checksum": "A" * 64,  # uppercase — rejected
                    "target_path": "/var/treadmill/repo-bin/cdk",
                },
            ],
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 422, response.text
    assert not session.committed
    assert store.configs == []


def test_get_repo_returns_worker_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()
    store.config_by_repo["owner/example"] = RepoConfig(
        repo="owner/example",
        worker_deps=WorkerDeps(
            python=["aws-cdk-lib==2.214.0"],
            binaries=[
                BinarySpec(
                    name="cdk",
                    download_url="https://example.com/cdk",
                    sha256_checksum="a" * 64,
                    target_path="/var/treadmill/repo-bin/cdk",
                )
            ],
        ),
    )
    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/onboarding/repos/owner/example")

    assert response.status_code == 200, response.text
    assert response.json()["worker_deps"] == {
        "python": ["aws-cdk-lib==2.214.0"],
        "node": [],
        "binaries": [
            {
                "name": "cdk",
                "download_url": "https://example.com/cdk",
                "sha256_checksum": "a" * 64,
                "target_path": "/var/treadmill/repo-bin/cdk",
            }
        ],
    }


def test_get_repo_returns_empty_worker_deps_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored config with ``worker_deps=None`` still returns an empty
    :class:`WorkerDeps` on the wire — the handler never serializes
    ``None`` for the field (ADR-0059)."""
    session = _StubSession()
    store = _FakeStore()
    store.config_by_repo["owner/example"] = RepoConfig(repo="owner/example")
    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/onboarding/repos/owner/example")

    assert response.status_code == 200, response.text
    assert response.json()["worker_deps"] == {
        "python": [],
        "node": [],
        "binaries": [],
    }


# ── ADR-0076: git author override ───────────────────────────────────────────


def test_onboard_repo_accepts_git_author_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0076: POST forwards git author override to the persisted RepoConfig."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "osmoai/osmo",
        "profile": {"languages": ["python"]},
        "git_author_name": "Joe Lepper",
        "git_author_email": "josephlepper@gmail.com",
        "commit_trailer": "",
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["git_author_name"] == "Joe Lepper"
    assert payload["git_author_email"] == "josephlepper@gmail.com"
    assert payload["commit_trailer"] == ""
    assert store.configs[0].git_author_name == "Joe Lepper"
    assert store.configs[0].git_author_email == "josephlepper@gmail.com"
    assert store.configs[0].commit_trailer == ""


def test_onboard_repo_accepts_git_author_override_with_trailer_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0076: custom trailer text is accepted and returned."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "example/repo",
        "profile": {"languages": ["go"]},
        "git_author_name": "Bot User",
        "git_author_email": "bot@example.com",
        "commit_trailer": "Custom-Trailer: value",
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["commit_trailer"] == "Custom-Trailer: value"
    assert store.configs[0].commit_trailer == "Custom-Trailer: value"


def test_onboard_repo_rejects_name_without_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0076: Pydantic validator rejects unpaired name/email."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "example/repo",
        "profile": {"languages": ["python"]},
        "git_author_name": "Joe Lepper",
        "git_author_email": None,
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 422, response.text
    assert not session.committed
    assert store.configs == []


def test_onboard_repo_rejects_email_without_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0076: Pydantic validator rejects unpaired email/name."""
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    body = {
        "repo": "example/repo",
        "profile": {"languages": ["python"]},
        "git_author_name": None,
        "git_author_email": "josephlepper@gmail.com",
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/onboarding/repos", json=body)

    assert response.status_code == 422, response.text
    assert not session.committed
    assert store.configs == []


def test_get_repo_returns_git_author_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0076: GET returns git author override fields."""
    session = _StubSession()
    store = _FakeStore()
    store.config_by_repo["example/repo"] = RepoConfig(
        repo="example/repo",
        git_author_name="Joe Lepper",
        git_author_email="josephlepper@gmail.com",
        commit_trailer="",
    )
    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/onboarding/repos/example/repo")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["git_author_name"] == "Joe Lepper"
    assert payload["git_author_email"] == "josephlepper@gmail.com"
    assert payload["commit_trailer"] == ""


def test_get_repo_returns_none_for_unset_git_author_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0076: GET returns None for unset git author override fields."""
    session = _StubSession()
    store = _FakeStore()
    store.config_by_repo["example/repo"] = RepoConfig(repo="example/repo")
    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/onboarding/repos/example/repo")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["git_author_name"] is None
    assert payload["git_author_email"] is None
    assert payload["commit_trailer"] is None
