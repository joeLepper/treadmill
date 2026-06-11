"""Tests for treadmill onboarding update CLI command (ADR-0059 Step 5)."""

from __future__ import annotations

import json

import pytest
import typer
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app
from treadmill_cli.commands.onboarding import _merge_worker_deps, _parse_binary_spec

runner = CliRunner()

_REPO = "org/my-repo"
_GET_URL = f"http://fake-api/api/v1/onboarding/repos/{_REPO}"
_POST_URL = "http://fake-api/api/v1/onboarding/repos"

_VALID_SHA256 = "a" * 64
_VALID_TARGET = "/var/treadmill/repo-bin/mytool"
_VALID_BINARY_SPEC = f"mytool=https://example.com/mytool={_VALID_SHA256}@{_VALID_TARGET}"


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")


def _config_response(**overrides: object) -> dict:
    base: dict = {
        "repo": _REPO,
        "mode": "conform",
        "auto_merge_blocked": False,
        "test_command": None,
        "lint_command": None,
        "claude_account": None,
        "worker_deps": {"python": [], "node": [], "binaries": []},
    }
    base.update(overrides)
    return base


def _upsert_response(**overrides: object) -> dict:
    return _config_response(**overrides)


# ── _parse_binary_spec unit tests ─────────────────────────────────────────────


def test_parse_binary_spec_happy_path() -> None:
    result = _parse_binary_spec(_VALID_BINARY_SPEC)
    assert result["name"] == "mytool"
    assert result["download_url"] == "https://example.com/mytool"
    assert result["sha256_checksum"] == _VALID_SHA256
    assert result["target_path"] == _VALID_TARGET


def test_parse_binary_spec_missing_at_target() -> None:
    with pytest.raises(typer.Exit):
        _parse_binary_spec("mytool=https://example.com/mytool=aaaa")


def test_parse_binary_spec_missing_equals_separators() -> None:
    with pytest.raises(typer.Exit):
        _parse_binary_spec(f"mytool@{_VALID_TARGET}")


def test_parse_binary_spec_empty_name() -> None:
    with pytest.raises(typer.Exit):
        _parse_binary_spec(f"=https://example.com/mytool={_VALID_SHA256}@{_VALID_TARGET}")


def test_parse_binary_spec_invalid_sha256_length() -> None:
    with pytest.raises(typer.Exit):
        _parse_binary_spec(f"mytool=https://example.com/mytool=abcdef@{_VALID_TARGET}")


def test_parse_binary_spec_invalid_sha256_uppercase() -> None:
    with pytest.raises(typer.Exit):
        _parse_binary_spec(
            f"mytool=https://example.com/mytool={'A' * 64}@{_VALID_TARGET}"
        )


def test_parse_binary_spec_bad_target_prefix() -> None:
    with pytest.raises(typer.Exit):
        _parse_binary_spec(
            f"mytool=https://example.com/mytool={_VALID_SHA256}@/tmp/tool"
        )


# ── _merge_worker_deps unit tests ─────────────────────────────────────────────


def test_merge_worker_deps_additive_python() -> None:
    existing = {"python": ["boto3==1.0"], "node": [], "binaries": []}
    result = _merge_worker_deps(existing, ["aws-cdk-lib==2.214.0"], [], [])
    assert result["python"] == ["boto3==1.0", "aws-cdk-lib==2.214.0"]
    assert result["node"] == []
    assert result["binaries"] == []


def test_merge_worker_deps_dedup_python() -> None:
    existing = {"python": ["boto3==1.0"], "node": [], "binaries": []}
    result = _merge_worker_deps(existing, ["boto3==1.0", "requests"], [], [])
    assert result["python"] == ["boto3==1.0", "requests"]


def test_merge_worker_deps_additive_node() -> None:
    existing = {"python": [], "node": ["typescript@5"], "binaries": []}
    result = _merge_worker_deps(existing, [], ["@types/node@20"], [])
    assert result["node"] == ["typescript@5", "@types/node@20"]


def test_merge_worker_deps_additive_binaries() -> None:
    bin1 = {
        "name": "tool1",
        "download_url": "https://a.example/tool1",
        "sha256_checksum": "b" * 64,
        "target_path": "/var/treadmill/repo-bin/tool1",
    }
    bin2 = {
        "name": "tool2",
        "download_url": "https://a.example/tool2",
        "sha256_checksum": "c" * 64,
        "target_path": "/var/treadmill/repo-bin/tool2",
    }
    existing = {"python": [], "node": [], "binaries": [bin1]}
    result = _merge_worker_deps(existing, [], [], [bin2])
    assert len(result["binaries"]) == 2
    assert result["binaries"][0] == bin1
    assert result["binaries"][1] == bin2


def test_merge_worker_deps_dedup_binaries() -> None:
    bin1 = {
        "name": "tool1",
        "download_url": "https://a.example/tool1",
        "sha256_checksum": "b" * 64,
        "target_path": "/var/treadmill/repo-bin/tool1",
    }
    existing = {"python": [], "node": [], "binaries": [bin1]}
    result = _merge_worker_deps(existing, [], [], [bin1])
    assert len(result["binaries"]) == 1


def test_merge_worker_deps_empty_existing() -> None:
    result = _merge_worker_deps({}, ["pkg==1.0"], ["npm-pkg"], [])
    assert result["python"] == ["pkg==1.0"]
    assert result["node"] == ["npm-pkg"]


# ── CLI integration tests ─────────────────────────────────────────────────────


def test_update_python_adds_additively(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=_GET_URL,
        json=_config_response(
            worker_deps={"python": ["boto3==1.0"], "node": [], "binaries": []}
        ),
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST",
        url=_POST_URL,
        json=_upsert_response(),
        status_code=200,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-python", "aws-cdk-lib==2.214.0",
    ])
    assert result.exit_code == 0, result.output
    # The command reports "updated <repo>: python=N node=N binaries=N".
    assert f"updated {_REPO}" in result.output
    assert "python=2" in result.output

    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(post.content)
    assert body["worker_deps"]["python"] == ["boto3==1.0", "aws-cdk-lib==2.214.0"]


def test_update_multiple_python_flags(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_GET_URL, json=_config_response(), status_code=200,
    )
    httpx_mock.add_response(
        method="POST", url=_POST_URL, json=_upsert_response(), status_code=200,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-python", "pkgA==1.0",
        "--worker-deps-python", "pkgB==2.0",
    ])
    assert result.exit_code == 0, result.output
    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(post.content)
    assert body["worker_deps"]["python"] == ["pkgA==1.0", "pkgB==2.0"]


def test_update_node_adds_additively(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=_GET_URL,
        json=_config_response(
            worker_deps={"python": [], "node": ["typescript@5"], "binaries": []}
        ),
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST", url=_POST_URL, json=_upsert_response(), status_code=200,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-node", "@types/node@20",
    ])
    assert result.exit_code == 0, result.output
    assert "node=2" in result.output

    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(post.content)
    assert body["worker_deps"]["node"] == ["typescript@5", "@types/node@20"]


def test_update_binary_spec_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_GET_URL, json=_config_response(), status_code=200,
    )
    httpx_mock.add_response(
        method="POST", url=_POST_URL, json=_upsert_response(), status_code=200,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--binary", _VALID_BINARY_SPEC,
    ])
    assert result.exit_code == 0, result.output
    assert "binaries=1" in result.output

    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(post.content)
    assert len(body["worker_deps"]["binaries"]) == 1
    b = body["worker_deps"]["binaries"][0]
    assert b["name"] == "mytool"
    assert b["sha256_checksum"] == _VALID_SHA256
    assert b["target_path"] == _VALID_TARGET


def test_update_binary_spec_parse_error_exits_1() -> None:
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--binary", "badspec",
    ])
    assert result.exit_code == 1
    assert "invalid" in result.output.lower()


def test_clear_worker_deps(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=_GET_URL,
        json=_config_response(
            worker_deps={"python": ["boto3==1.0"], "node": ["ts@5"], "binaries": []}
        ),
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST", url=_POST_URL, json=_upsert_response(), status_code=200,
    )
    result = runner.invoke(app, ["onboarding", "update", _REPO, "--clear-worker-deps"])
    assert result.exit_code == 0, result.output
    assert "python=0" in result.output
    assert "node=0" in result.output
    assert "binaries=0" in result.output

    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(post.content)
    assert body["worker_deps"] == {"python": [], "node": [], "binaries": []}


def test_clear_worker_deps_mutually_exclusive_with_python() -> None:
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--clear-worker-deps",
        "--worker-deps-python", "boto3==1.0",
    ])
    assert result.exit_code == 1
    assert "cannot be combined" in result.output


def test_clear_worker_deps_mutually_exclusive_with_node() -> None:
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--clear-worker-deps",
        "--worker-deps-node", "ts@5",
    ])
    assert result.exit_code == 1
    assert "cannot be combined" in result.output


def test_clear_worker_deps_mutually_exclusive_with_binary() -> None:
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--clear-worker-deps",
        "--binary", _VALID_BINARY_SPEC,
    ])
    assert result.exit_code == 1
    assert "cannot be combined" in result.output


def test_get_repo_config_404_surfaces_clear_message(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=_GET_URL,
        json={"detail": f"repo {_REPO!r} is not onboarded"},
        status_code=404,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-python", "boto3==1.0",
    ])
    assert result.exit_code == 1
    assert "not registered" in result.output
    assert "treadmill onboarding add" in result.output


def test_upsert_422_surfaces_validation_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_GET_URL, json=_config_response(), status_code=200,
    )
    httpx_mock.add_response(
        method="POST",
        url=_POST_URL,
        json={"detail": "sha256_checksum must be exactly 64 lowercase hex characters"},
        status_code=422,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-python", "boto3==1.0",
    ])
    assert result.exit_code == 1
    assert "validation error" in result.output


def test_generic_api_error_on_get(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=_GET_URL,
        json={"detail": "internal server error"},
        status_code=500,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-python", "boto3==1.0",
    ])
    assert result.exit_code == 2
    assert "500" in result.output


def test_generic_api_error_on_post(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_GET_URL, json=_config_response(), status_code=200,
    )
    httpx_mock.add_response(
        method="POST",
        url=_POST_URL,
        json={"detail": "internal server error"},
        status_code=500,
    )
    result = runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-python", "boto3==1.0",
    ])
    assert result.exit_code == 2
    assert "500" in result.output


def test_post_body_preserves_mode_and_auto_merge_blocked(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=_GET_URL,
        json=_config_response(mode="adapt", auto_merge_blocked=True),
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST", url=_POST_URL, json=_upsert_response(), status_code=200,
    )
    runner.invoke(app, [
        "onboarding", "update", _REPO,
        "--worker-deps-python", "boto3==1.0",
    ])
    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(post.content)
    assert body["mode"] == "adapt"
    assert body["auto_merge_blocked"] is True
