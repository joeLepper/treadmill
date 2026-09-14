"""CLI foils for ``treadmill team reconcile`` (ADR-0109 watcher, step 4b-2).

The reconcile makes team liveness match work, autonomously, so the dangerous
directions are: reviving a team that should stay down (churn), tearing down one
with work (loss), and acting on an unconfirmable state. Each foil pins one branch.
The load-bearing ones (Ernie): re-standup fires on drain-not-clean (a resolved
escalation), liveness is the UNIT not the lease row (crash-heal), single-flight
flock, and teardown goes through the same drain-guard as the sweep.
"""

from __future__ import annotations

import fcntl
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from treadmill_cli.api_client import ApiError
from treadmill_cli.commands import team as team_module
from treadmill_cli.commands.team import team_app
from typer.testing import CliRunner

runner = CliRunner()

_NOW = datetime.now(UTC)
_OLD = (_NOW - timedelta(hours=48)).isoformat()
_RECENT = (_NOW - timedelta(minutes=5)).isoformat()


def _cfg(repo: str, lifecycle: str = "ephemeral") -> dict:
    slug = repo.replace("/", "-").lower()
    return {
        "repo": repo,
        "coordinator_label": f"coordinator-{slug}",
        "evaluator_label": f"evaluator-{slug}",
        "worker_labels": [f"worker-{slug}-1", f"worker-{slug}-2", f"worker-{slug}-3"],
        "lifecycle": lifecycle,
        "merge_target": "feature-branch",
    }


def _drain(clean: bool, last_activity_at: str | None = _OLD) -> dict:
    return {
        "repo": "x",
        "clean": clean,
        "blocking": [],
        "parked": [],
        "last_activity_at": last_activity_at,
    }


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch):
    fake = MagicMock()
    state: dict = {"configs": [], "drains": {}}

    def _request(method: str, path: str, **_):
        if path == "/api/v1/team_configs":
            return state["configs"]
        if path.endswith("/drain"):
            repo = path[len("/api/v1/team_configs/") : -len("/drain")]
            val = state["drains"][repo]
            if isinstance(val, Exception):
                raise val
            return val
        raise AssertionError(f"unexpected request {method} {path}")

    fake._request.side_effect = _request
    fake._state = state

    class _Factory:
        def __init__(self, _config):
            pass

        def __enter__(self_inner):
            return fake

        def __exit__(self_inner, *args):
            return False

    monkeypatch.setattr(team_module, "ApiClient", _Factory)
    monkeypatch.setattr(
        team_module, "load_config", lambda: MagicMock(api_url="http://x", api_key=None)
    )
    return fake


@pytest.fixture
def systemctl(monkeypatch: pytest.MonkeyPatch):
    """Configurable systemctl fake. `.live` is the set of coordinator labels whose
    is-active returns 0 (active); enable/start/disable calls are recorded."""
    calls: list[list[str]] = []
    live: set[str] = set()

    def _fake_run(argv, **_):
        result = MagicMock()
        result.stderr = ""
        if argv[:1] == ["is-active"] or argv[0] == "is-active":
            # argv is the systemctl args (no "systemctl --user" prefix here).
            unit = argv[-1]
            result.returncode = 0 if any(f"@{lbl}." in unit for lbl in live) else 3
            return result
        calls.append(argv)
        result.returncode = 0
        return result

    monkeypatch.setattr(
        team_module.subprocess,
        "run",
        lambda a, **k: _fake_run(a[2:], **k)
        if a[:2] == ["systemctl", "--user"]
        else _fake_run(a, **k),
    )
    holder = MagicMock()
    holder.calls = calls
    holder.live = live
    return holder


@pytest.fixture(autouse=True)
def tmp_lock(monkeypatch: pytest.MonkeyPatch, tmp_path):
    lock = tmp_path / "team-reconcile.lock"
    monkeypatch.setattr(team_module, "_RECONCILE_LOCK", lock)
    return lock


def _run(fake_api, configs, drains, *args):
    fake_api._state["configs"] = configs
    fake_api._state["drains"] = drains
    result = runner.invoke(team_app, ["reconcile", *args])
    assert result.exit_code == 0, result.output
    return result


def _acted(systemctl, verb: str) -> list[list[str]]:
    return [c for c in systemctl.calls if c and c[0] == verb]


def test_not_live_with_work_is_revived(fake_api, systemctl) -> None:
    """Not live + drain not-clean (has work) → STAND UP (enable+start). Covers the
    resolved-escalation re-standup AND the crashed-standup crash-heal — both present
    as 'unit down, work exists' (Ernie #1 + #2)."""
    # coordinator NOT in live set → is-active returns inactive.
    _run(fake_api, [_cfg("o/work")], {"o/work": _drain(clean=False)})
    # enable + start fired for all 5 labels.
    assert len(_acted(systemctl, "enable")) == 5
    assert len(_acted(systemctl, "start")) == 5
    assert _acted(systemctl, "disable") == []


def test_live_with_work_is_left_alone(fake_api, systemctl) -> None:
    """Live + has work → the team is working; do nothing."""
    systemctl.live.add("coordinator-o-live")
    _run(fake_api, [_cfg("o/live")], {"o/live": _drain(clean=False)})
    assert systemctl.calls == []  # no enable/start/disable — only is-active


def test_clean_and_idle_live_team_is_torn_down(fake_api, systemctl) -> None:
    """Live + drain clean + idle beyond grace (ephemeral) → tear down (same
    drain-guard as sweep)."""
    systemctl.live.add("coordinator-o-idle")
    _run(fake_api, [_cfg("o/idle")], {"o/idle": _drain(clean=True, last_activity_at=_OLD)})
    assert len(_acted(systemctl, "disable")) == 5
    assert _acted(systemctl, "start") == []


def test_clean_not_live_is_left_down(fake_api, systemctl) -> None:
    """Clean + already down → nothing to do (not revived: no work; not torn down:
    already down)."""
    _run(fake_api, [_cfg("o/done")], {"o/done": _drain(clean=True, last_activity_at=_OLD)})
    assert systemctl.calls == []


def test_parked_only_clean_team_not_revived(fake_api, systemctl) -> None:
    """A resolved-escalation re-standup fires on drain-NOT-clean; a still-parked
    escalation reads drain-CLEAN (parked-on-human), so a down team with only a
    parked task is NOT revived — it waits for the human (Ernie #1)."""
    # clean=True models 'only parked work' (drain treats escalated as green).
    _run(fake_api, [_cfg("o/parked")], {"o/parked": _drain(clean=True, last_activity_at=_RECENT)})
    assert systemctl.calls == []  # not revived, not torn down (within grace)


def test_manual_team_is_never_managed(fake_api, systemctl) -> None:
    """A manual team is never auto-revived or auto-torn-down."""
    _run(fake_api, [_cfg("o/man", lifecycle="manual")], {"o/man": _drain(clean=False)})
    assert systemctl.calls == []  # not even is-active matters; no action


def test_drain_error_leaves_team_as_is(fake_api, systemctl) -> None:
    """Fail-closed: an unconfirmable drain neither revives nor tears down."""
    systemctl.live.add("coordinator-o-err")
    _run(fake_api, [_cfg("o/err")], {"o/err": ApiError(500, "boom")})
    assert systemctl.calls == []


def test_dry_run_touches_no_systemd(fake_api, systemctl) -> None:
    fake_api._state["configs"] = [_cfg("o/work")]
    fake_api._state["drains"] = {"o/work": _drain(clean=False)}
    result = runner.invoke(team_app, ["reconcile", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert _acted(systemctl, "enable") == []
    assert "o/work" in result.output


def test_single_flight_skips_when_lock_held(fake_api, systemctl, tmp_lock) -> None:
    """A concurrent reconcile is a no-op: if the host lock is already held, the pass
    skips without touching systemd (Ernie #3)."""
    fake_api._state["configs"] = [_cfg("o/work")]
    fake_api._state["drains"] = {"o/work": _drain(clean=False)}
    holder = open(tmp_lock, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = runner.invoke(team_app, ["reconcile"])
        assert result.exit_code == 0, result.output
        assert systemctl.calls == []  # never acted — lock was held
        assert "already running" in result.output
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_live_persistent_clean_team_reads_as_running_not_down(
    fake_api, systemctl
) -> None:
    """Cosmetic-correctness (Ernie 4b-2): a LIVE persistent+clean team is not swept
    (persistent) and must report as running, not 'left down' — and nothing touches
    systemd."""
    systemctl.live.add("coordinator-o-persist")
    result = _run(
        fake_api,
        [_cfg("o/persist", lifecycle="persistent")],
        {"o/persist": _drain(clean=True, last_activity_at=_OLD)},
    )
    assert systemctl.calls == []
    out = result.output
    # The repo appears under "left running", not under "left down".
    running_line = next(ln for ln in out.splitlines() if "left running" in ln)
    down_line = next(ln for ln in out.splitlines() if "left down" in ln)
    assert "o/persist" in running_line
    assert "o/persist" not in down_line
