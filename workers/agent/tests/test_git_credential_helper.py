"""Tests for the on-demand git credential helper.

Covers :mod:`treadmill_agent.git_credential_helper` — the per-operation
mint that supersedes ``gh auth setup-git``'s static-token helper for
github.com (so long builds don't outlive the token gh was handed at
startup).
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest import mock

import pytest

from treadmill_agent import git_credential_helper


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _set_stdin(monkeypatch: pytest.MonkeyPatch, contents: str) -> None:
    monkeypatch.setattr(git_credential_helper.sys, "stdin", io.StringIO(contents))


def _capture_stdout_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(git_credential_helper.sys, "stdout", out)
    monkeypatch.setattr(git_credential_helper.sys, "stderr", err)
    return out, err


# ── Happy path ─────────────────────────────────────────────────────────────


def test_get_for_github_mints_and_prints_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get`` for ``host=github.com`` mints via the API and prints the
    ``x-access-token`` / token credential pair plus a terminating blank
    line per git's credential protocol."""
    seen: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        return _FakeResp(json.dumps({"token": "ghs_fresh"}).encode())

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    monkeypatch.setenv("TREADMILL_API_URL", "http://api:9/")
    _set_stdin(
        monkeypatch,
        "protocol=https\nhost=github.com\npath=owner/repo.git\n\n",
    )
    out, err = _capture_stdout_stderr(monkeypatch)

    rc = git_credential_helper.main(["git_credential_helper.py", "get"])

    assert rc == 0
    assert seen["url"] == "http://api:9/api/v1/github/installation-token"
    assert seen["method"] == "POST"
    # ``.git`` suffix stripped; owner/name passed to the API for scoping.
    assert json.loads(seen["body"]) == {"repo": "owner/repo"}
    assert out.getvalue() == "username=x-access-token\npassword=ghs_fresh\n\n"
    assert err.getvalue() == ""


def test_get_strips_git_suffix_and_extra_path_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the first two path segments form ``owner/name``; trailing
    segments (e.g. ``info/refs``) and the ``.git`` suffix are stripped."""
    seen: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        seen["body"] = req.data
        return _FakeResp(json.dumps({"token": "ghs_x"}).encode())

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    _set_stdin(
        monkeypatch,
        "protocol=https\nhost=github.com\npath=owner/repo.git/info/refs\n\n",
    )
    _capture_stdout_stderr(monkeypatch)

    git_credential_helper.main(["x", "get"])
    assert json.loads(seen["body"]) == {"repo": "owner/repo"}


def test_get_without_path_posts_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable ``path`` → POST an empty body. Mirrors the startup
    home-installation mint in :func:`bootstrap_github_auth_via_app`."""
    seen: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        seen["body"] = req.data
        return _FakeResp(json.dumps({"token": "ghs_home"}).encode())

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    _set_stdin(monkeypatch, "protocol=https\nhost=github.com\n\n")
    out, _ = _capture_stdout_stderr(monkeypatch)

    git_credential_helper.main(["x", "get"])
    assert json.loads(seen["body"]) == {}
    assert "password=ghs_home" in out.getvalue()


def test_get_uses_default_api_url_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default API URL matches :func:`treadmill_agent.config.load`'s
    fallback (``http://treadmill-api:8088``)."""
    seen: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        seen["url"] = req.full_url
        return _FakeResp(json.dumps({"token": "ghs_x"}).encode())

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    monkeypatch.delenv("TREADMILL_API_URL", raising=False)
    _set_stdin(monkeypatch, "protocol=https\nhost=github.com\n\n")
    _capture_stdout_stderr(monkeypatch)

    git_credential_helper.main(["x", "get"])
    assert seen["url"] == "http://treadmill-api:8088/api/v1/github/installation-token"


# ── Non-github / non-get: silent no-op ─────────────────────────────────────


def test_non_github_host_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """For ``host != github.com``, exit 0 with no output AND never call the
    API — the helper is registered globally so it must not exfiltrate
    credential requests for other hosts."""
    called: list[Any] = []

    def fake_urlopen(*a, **kw):  # type: ignore[no-untyped-def]
        called.append((a, kw))
        return _FakeResp(b"{}")

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    _set_stdin(monkeypatch, "protocol=https\nhost=gitlab.com\n\n")
    out, err = _capture_stdout_stderr(monkeypatch)

    rc = git_credential_helper.main(["x", "get"])
    assert rc == 0
    assert out.getvalue() == ""
    assert err.getvalue() == ""
    assert called == []


def test_store_and_erase_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    """``store`` / ``erase`` exit 0 silently (nothing is cached); the API
    must not be called for these actions."""
    called: list[Any] = []

    def fake_urlopen(*a, **kw):  # type: ignore[no-untyped-def]
        called.append((a, kw))
        return _FakeResp(b"{}")

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    _set_stdin(monkeypatch, "protocol=https\nhost=github.com\n\n")
    out, err = _capture_stdout_stderr(monkeypatch)

    assert git_credential_helper.main(["x", "store"]) == 0
    assert git_credential_helper.main(["x", "erase"]) == 0
    assert called == []
    assert out.getvalue() == ""
    assert err.getvalue() == ""


def test_missing_action_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No action arg → no-op (exit 0, no stdout)."""
    _set_stdin(monkeypatch, "")
    out, _ = _capture_stdout_stderr(monkeypatch)
    assert git_credential_helper.main(["x"]) == 0
    assert out.getvalue() == ""


# ── Failure modes: never crash a git operation ─────────────────────────────


def test_api_network_error_exits_zero_with_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mandate: a credential helper must NEVER break the git command
    that invoked it. Network errors → log to stderr, exit 0, no stdout."""
    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise OSError("connection refused")

    monkeypatch.setattr(git_credential_helper.urllib.request, "urlopen", boom)
    _set_stdin(monkeypatch, "protocol=https\nhost=github.com\n\n")
    out, err = _capture_stdout_stderr(monkeypatch)

    rc = git_credential_helper.main(["x", "get"])
    assert rc == 0
    assert out.getvalue() == ""
    assert "treadmill git-credential-helper failed" in err.getvalue()


def test_missing_token_in_response_exits_zero_with_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(b"{}"),
    )
    _set_stdin(monkeypatch, "protocol=https\nhost=github.com\n\n")
    out, err = _capture_stdout_stderr(monkeypatch)

    rc = git_credential_helper.main(["x", "get"])
    assert rc == 0
    assert out.getvalue() == ""
    assert "treadmill git-credential-helper failed" in err.getvalue()


def test_unexpected_exception_exits_zero_with_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an exception outside the named-error tuple (e.g. a programming
    bug in ``_mint_token``) must not propagate — git would otherwise see a
    non-zero exit and fail the operation."""
    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected")

    monkeypatch.setattr(git_credential_helper, "_mint_token", boom)
    _set_stdin(monkeypatch, "protocol=https\nhost=github.com\n\n")
    out, err = _capture_stdout_stderr(monkeypatch)

    rc = git_credential_helper.main(["x", "get"])
    assert rc == 0
    assert out.getvalue() == ""
    assert "treadmill git-credential-helper" in err.getvalue()


def test_token_never_in_argv_or_stderr_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the API returns a token but a downstream step fails, the token
    must not land on stderr (which is logged) or in argv (which we never
    construct from it). Pin this so a future refactor that, say, embeds
    the token in an error message trips a test."""
    sentinel = "ghs_SENSITIVE_TOKEN_DO_NOT_LEAK"

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        return _FakeResp(json.dumps({"token": sentinel}).encode())

    # Make stdout writes fail after token is in hand so an exception
    # carries a path through the error sink.
    class _BlowUp(io.StringIO):
        def write(self, _: str) -> int:
            raise OSError("disk full")

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    monkeypatch.setattr(git_credential_helper.sys, "stdout", _BlowUp())
    err = io.StringIO()
    monkeypatch.setattr(git_credential_helper.sys, "stderr", err)
    _set_stdin(monkeypatch, "protocol=https\nhost=github.com\n\n")

    rc = git_credential_helper.main(["x", "get"])
    assert rc == 0
    assert sentinel not in err.getvalue()


# ── Stdin parsing ──────────────────────────────────────────────────────────


def test_blank_line_terminates_attribute_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines after the first blank are ignored (e.g. junk trailing input
    from a misbehaving caller); the helper still produces clean credentials."""
    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        return _FakeResp(json.dumps({"token": "ghs_x"}).encode())

    monkeypatch.setattr(
        git_credential_helper.urllib.request, "urlopen", fake_urlopen,
    )
    _set_stdin(
        monkeypatch,
        "protocol=https\nhost=github.com\n\nGARBAGE_AFTER_BLANK\n",
    )
    out, _ = _capture_stdout_stderr(monkeypatch)

    assert git_credential_helper.main(["x", "get"]) == 0
    assert "password=ghs_x" in out.getvalue()


# ── Installation wire-up ───────────────────────────────────────────────────


def test_install_helper_runs_replace_all_for_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_install_git_credential_helper`` runs ``git config --global
    --replace-all`` so it supersedes the entry ``gh auth setup-git`` just
    wrote for github.com. The helper string never embeds a token."""
    from treadmill_agent import startup_auth

    calls: list[list[str]] = []

    def fake_run(args, **kw):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        return mock.MagicMock(returncode=0, stderr=b"")

    monkeypatch.setattr(startup_auth.subprocess, "run", fake_run)
    startup_auth._install_git_credential_helper()

    assert calls[0][:5] == [
        "git", "config", "--global", "--replace-all",
        "credential.https://github.com.helper",
    ]
    assert calls[0][5] == "!python -m treadmill_agent.git_credential_helper"
    # useHttpPath=true so the helper sees the ``path`` attribute on input
    # and can scope the mint to the right installation.
    assert calls[1] == [
        "git", "config", "--global",
        "credential.https://github.com.useHttpPath", "true",
    ]


def test_install_helper_raises_on_git_config_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from treadmill_agent import startup_auth

    monkeypatch.setattr(
        startup_auth.subprocess, "run",
        lambda args, **kw: mock.MagicMock(returncode=1, stderr=b"bad"),
    )
    with pytest.raises(startup_auth.StartupAuthError, match="git config"):
        startup_auth._install_git_credential_helper()


def test_bootstrap_via_app_calls_install_helper_after_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The credential helper override must run AFTER ``_apply_token_to_gh``
    (so gh's setup-git has already written its entry, which we then replace).
    Pin the ordering so a future refactor that swaps the calls doesn't
    silently leave the static-token helper in place."""
    from types import SimpleNamespace

    from treadmill_agent import startup_auth

    order: list[str] = []

    def fake_apply(token: str) -> None:
        order.append("apply")

    def fake_install() -> None:
        order.append("install")

    monkeypatch.setattr(
        startup_auth.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(
            json.dumps({"token": "ghs_x"}).encode()
        ),
    )
    monkeypatch.setattr(startup_auth, "_apply_token_to_gh", fake_apply)
    monkeypatch.setattr(
        startup_auth, "_install_git_credential_helper", fake_install,
    )

    startup_auth.bootstrap_github_auth_via_app(
        settings=SimpleNamespace(api_url="http://api:9"),
    )
    assert order == ["apply", "install"]
