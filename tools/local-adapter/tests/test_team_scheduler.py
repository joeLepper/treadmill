"""Tests for the ADR-0091 team-scheduler daemon (task 9d6c0658).

The decision logic lives in the API (``routers/scheduler.py``, its own
suite); these tests pin the DAEMON's enactment contract:

  * LOAD-BEARING FAIL-SAFE: unreachable / error / malformed / null
    decision -> ZERO team-control calls (never a fleet-wide pause on a
    missing decision — the API is a SPOF, plan 992d65b7).
  * The pause set structurally excludes ``desired_team`` even when the
    endpoint lists it quiescent (carried review note 1).
  * Anti-flap dwell: at most one switch per window, persisted across
    daemon restarts; and the dwell default is asserted <= the API's
    ``AGING_TIME_CONSTANT_MINUTES`` IMPORTED DIRECTLY (carried note 2 —
    the endpoint-side floor pin is slacker than the constant).
  * Single-active invariant over progress: the desired team is not
    activated while another team is still running (non-quiescent or
    failed pause).
  * Slug casing normalized on intake (carried note 4).
  * Single-instance flock (#333 class).
"""

from __future__ import annotations

from pathlib import Path

from treadmill_local.team_scheduler import (
    DEFAULT_DWELL_MINUTES,
    TeamScheduler,
    acquire_scheduler_lock,
    _team_label_dirs,
)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[tuple[str, str]] = set()

    def __call__(self, verb: str, slug: str) -> bool:
        self.calls.append((verb, slug))
        return (verb, slug) not in self.fail_on

    @property
    def pauses(self) -> list[str]:
        return [s for v, s in self.calls if v == "pause"]

    @property
    def activations(self) -> list[str]:
        return [s for v, s in self.calls if v == "activate"]


def _scheduler(
    decision,
    *,
    installed=("team-a", "team-b"),
    active=("team-a",),
    recorder: Recorder | None = None,
    dwell_minutes: float = 0.0,
    state_path: Path | None = None,
    now=None,
) -> tuple[TeamScheduler, Recorder]:
    recorder = recorder or Recorder()
    active_set = {a.lower() for a in active}
    kwargs = dict(
        fetch_decision=(
            decision if callable(decision) else (lambda: decision)
        ),
        team_control=recorder,
        installed_teams=lambda: installed,
        team_active=lambda slug: slug in active_set,
        dwell_minutes=dwell_minutes,
        state_path=state_path,
    )
    if now is not None:
        kwargs["now"] = now
    return TeamScheduler(**kwargs), recorder


# ── the load-bearing fail-safe ───────────────────────────────────────


def test_unreachable_decision_holds_everything() -> None:
    s, rec = _scheduler(None)  # fetch returns None = unreachable/error
    s.reconcile_once()
    assert rec.calls == []


def test_null_desired_team_holds_everything() -> None:
    s, rec = _scheduler({"desired_team": None, "quiescent_teams": ["team-a"]})
    s.reconcile_once()
    assert rec.calls == []


def test_malformed_decision_holds_everything() -> None:
    for bad in (
        {"desired_team": 42, "quiescent_teams": []},
        {"desired_team": "team-b", "quiescent_teams": "not-a-list"},
        {},
    ):
        s, rec = _scheduler(bad)
        s.reconcile_once()
        assert rec.calls == [], f"calls made on malformed decision {bad!r}"


def test_uninstalled_desired_team_holds() -> None:
    """The daemon can only enact teams under the teams root — a desired
    team with no install must not trigger pauses of the running one."""
    s, rec = _scheduler(
        {"desired_team": "team-zz", "quiescent_teams": ["team-a"]},
    )
    s.reconcile_once()
    assert rec.calls == []


# ── reconcile: switch, exclusion, single-active ──────────────────────


def test_switch_pauses_quiescent_current_then_activates_desired() -> None:
    s, rec = _scheduler(
        {"desired_team": "team-b", "quiescent_teams": ["team-a"]},
    )
    s.reconcile_once()
    assert rec.calls == [("pause", "team-a"), ("activate", "team-b")]


def test_desired_team_never_paused_even_when_listed_quiescent() -> None:
    """Carried note 1: the endpoint reports facts (a desired team
    between dispatches IS quiescent); the daemon enacts policy."""
    s, rec = _scheduler(
        {"desired_team": "team-a", "quiescent_teams": ["team-a"]},
        active=("team-a",),
    )
    s.reconcile_once()
    assert rec.pauses == []  # steady state: no calls at all
    assert rec.calls == []


def test_non_quiescent_current_blocks_pause_and_activation() -> None:
    """Single-active invariant over progress: while team-a runs
    non-quiescent, team-b is NOT brought up alongside it."""
    s, rec = _scheduler(
        {"desired_team": "team-b", "quiescent_teams": []},
    )
    s.reconcile_once()
    assert rec.calls == []


def test_failed_pause_blocks_activation() -> None:
    rec = Recorder()
    rec.fail_on.add(("pause", "team-a"))
    s, rec = _scheduler(
        {"desired_team": "team-b", "quiescent_teams": ["team-a"]},
        recorder=rec,
    )
    s.reconcile_once()
    assert rec.pauses == ["team-a"]
    assert rec.activations == []


def test_idle_fleet_activation_is_immediate() -> None:
    """Nothing active -> activating pauses nobody, so it is not
    dwell-gated (tenure protection guards the CURRENT team only)."""
    s, rec = _scheduler(
        {"desired_team": "team-b", "quiescent_teams": []},
        active=(),
    )
    s.reconcile_once()
    assert rec.calls == [("activate", "team-b")]


def test_slug_casing_normalized_on_intake() -> None:
    """Carried note 4: API-returned slugs and local install names are
    both lowered before comparison — a casing drift can't hide an
    active team from the reconciler."""
    s, rec = _scheduler(
        {"desired_team": "Team-B", "quiescent_teams": ["TEAM-A"]},
        installed=("Team-A", "team-b"),
        active=("team-a",),
    )
    s.reconcile_once()
    assert rec.calls == [("pause", "team-a"), ("activate", "team-b")]


# ── anti-flap dwell ──────────────────────────────────────────────────


def test_dwell_blocks_second_switch_within_window(tmp_path: Path) -> None:
    clock = {"t": 1000.0}
    state = tmp_path / "state.json"

    def make(decision, active):
        return _scheduler(
            decision, active=active,
            dwell_minutes=20, state_path=state, now=lambda: clock["t"],
        )

    # Switch 1: a -> b (no prior stamp; pausing is allowed, stamps tenure).
    s, rec = make({"desired_team": "team-b", "quiescent_teams": ["team-a"]},
                  ("team-a",))
    s.reconcile_once()
    assert rec.calls == [("pause", "team-a"), ("activate", "team-b")]

    # 5 min later the decision flips back: dwell (20 min) must HOLD —
    # even though team-b is quiescent.
    clock["t"] += 5 * 60
    s2, rec2 = make({"desired_team": "team-a", "quiescent_teams": ["team-b"]},
                    ("team-b",))
    s2.reconcile_once()
    assert rec2.calls == []

    # Past the window the switch proceeds.
    clock["t"] += 16 * 60
    s3, rec3 = make({"desired_team": "team-a", "quiescent_teams": ["team-b"]},
                    ("team-b",))
    s3.reconcile_once()
    assert rec3.calls == [("pause", "team-b"), ("activate", "team-a")]


def test_dwell_stamp_persists_across_daemon_restart(tmp_path: Path) -> None:
    """The stamp lives in the state file: a NEW TeamScheduler (daemon
    restart) inherits the tenure window instead of flapping."""
    clock = {"t": 5000.0}
    state = tmp_path / "state.json"
    s, rec = _scheduler(
        {"desired_team": "team-b", "quiescent_teams": ["team-a"]},
        dwell_minutes=20, state_path=state, now=lambda: clock["t"],
    )
    s.reconcile_once()
    assert rec.activations == ["team-b"]

    clock["t"] += 60  # 1 min later, "restarted" daemon, flipped decision
    s2, rec2 = _scheduler(
        {"desired_team": "team-a", "quiescent_teams": ["team-b"]},
        active=("team-b",),
        dwell_minutes=20, state_path=state, now=lambda: clock["t"],
    )
    s2.reconcile_once()
    assert rec2.calls == []


def test_default_dwell_does_not_exceed_the_aging_constant() -> None:
    """Carried note 2, asserted against the IMPORTED constant (the
    #344 endpoint suite pins only a floor, which is slacker): aging
    must never demand swaps faster than anti-flap permits one."""
    from treadmill_api.routers.scheduler import AGING_TIME_CONSTANT_MINUTES

    assert DEFAULT_DWELL_MINUTES <= AGING_TIME_CONSTANT_MINUTES


# ── single-instance lock (#333 class) ────────────────────────────────


def test_second_lock_acquire_refused(tmp_path: Path) -> None:
    lock = tmp_path / "team-scheduler.lock"
    fd1 = acquire_scheduler_lock(lock)
    assert fd1 is not None
    # flock conflicts across open file descriptions even in-process.
    assert acquire_scheduler_lock(lock) is None
    import os

    os.close(fd1)
    fd3 = acquire_scheduler_lock(lock)
    assert fd3 is not None  # released on close -> acquirable again
    os.close(fd3)


# ── label enumeration mirrors team-control ───────────────────────────


def test_team_label_dirs_accept_only_team_shapes(tmp_path: Path) -> None:
    root = tmp_path / "teams"
    team = root / "team-a"
    for d in (
        "coordinator-team-a", "evaluator-team-a",
        "worker-team-a-1", "worker-team-a-12",
        "worker-team-a-x",        # non-numeric suffix: ignored
        "coordinator-other",      # different slug: ignored
        "treadmill-alan",         # orchestrator shape: ignored
    ):
        (team / d).mkdir(parents=True)
    labels = _team_label_dirs(root, "team-a")
    assert labels == [
        "coordinator-team-a",
        "evaluator-team-a",
        "worker-team-a-1",
        "worker-team-a-12",
    ]
