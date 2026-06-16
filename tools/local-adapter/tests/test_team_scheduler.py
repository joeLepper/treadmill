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

from treadmill_local.account_pool import AccountLeasePool
from treadmill_local.team_scheduler import (
    DEFAULT_DWELL_MINUTES,
    TeamScheduler,
    acquire_scheduler_lock,
    default_team_control_path,
    _team_label_dirs,
)


def _mk_lease_pool(accounts):
    """An in-memory lease pool whose 'on-disk binding' is a dict, for the
    reconcile-from-ground-truth path. Returns (pool, bound-dict)."""
    bound: dict[str, str] = {}
    return (
        AccountLeasePool(
            accounts=accounts,
            state_path=None,
            bind_account=lambda t, a: bound.__setitem__(t, a),
            read_bound_account=lambda t: bound.get(t),
        ),
        bound,
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
    max_active_teams: int = 1,
    account_pool=(),
    lease_pool=None,
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
        max_active_teams=max_active_teams,
        account_pool=account_pool,
        lease_pool=lease_pool,
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


# ── default control-script path (task 1518598a) ─────────────────────


def test_default_team_control_path_resolves_on_the_real_tree() -> None:
    """Task 1518598a regression: the original default used parents[3]
    (the repo root) and resolved to <repo>/cc-channels/systemd —
    missing the tools/ segment — so the daemon exited 1 unless
    TREADMILL_TEAM_CONTROL overrode it. This test STATS the real tree
    (the plan's coarse grep gate never did): the default must point at
    an existing, executable file at tools/cc-channels/systemd/.
    """
    import os

    p = default_team_control_path()
    assert p.is_file(), f"default team-control path does not exist: {p}"
    assert os.access(p, os.X_OK), f"not executable: {p}"
    assert p.parts[-4:] == (
        "tools", "cc-channels", "systemd", "treadmill-team-control",
    ), p


def test_team_scheduler_unit_runs_daemon_and_restarts_on_failure() -> None:
    """The durable-service half of task 1518598a: the shipped --user
    unit must run the daemon module, restart on FAILURE only (a flock
    refusal exits 0 = success, so a second instance never restart-loops
    against the live one), and record SIGTERM's 143 as success (the
    #343 papercut class)."""
    unit = (
        Path(__file__).resolve().parents[2]
        / "cc-channels" / "systemd" / "treadmill-team-scheduler.service"
    )
    body = unit.read_text()
    assert "-m treadmill_local.team_scheduler" in body
    assert "Restart=on-failure" in body
    assert "SuccessExitStatus=143" in body
    assert "WantedBy=default.target" in body


# ── ADR-0092 multi-active: N teams, pool-sized, per-team dwell ────────
#
# The single-active suite above runs UNCHANGED at the default
# (max_active_teams=1, empty pool) and is the no-regression gate. These
# add the multi-active behavior.


def test_two_desired_teams_activate_concurrently_with_pool() -> None:
    """max=2 + a 2-account pool: both top desired teams come up together."""
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        active=(),
        max_active_teams=2,
        account_pool=("acct1", "acct2"),
    )
    s.reconcile_once()
    assert rec.activations == ["team-a", "team-b"]  # rank order
    assert rec.pauses == []


def test_no_pool_clamps_multi_active_to_one() -> None:
    """The safe-staging clamp: max=2 but NO pool configured -> effective
    cap is 1, so only the top team activates (never two on one default
    subscription = the double-burn). This is what makes task B safe to
    ship before the pool (task C) lands."""
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        active=(),
        max_active_teams=2,
        account_pool=(),  # no pool
    )
    s.reconcile_once()
    assert rec.activations == ["team-a"]


def test_pool_size_caps_effective_max_below_max_active() -> None:
    """effective cap = min(max_active_teams, pool_size): a 1-account pool
    holds it to one even at max=2."""
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        active=(),
        max_active_teams=2,
        account_pool=("only-one",),
    )
    s.reconcile_once()
    assert rec.activations == ["team-a"]


def test_singular_desired_team_fallback_drives_single_slot() -> None:
    """A decision with only the singular ``desired_team`` (pre-task-A API,
    or the unchanged fixtures) drives the single-slot path even at max=2 —
    one desired team yields one activation."""
    s, rec = _scheduler(
        {"desired_team": "team-b", "quiescent_teams": ["team-a"]},
        active=("team-a",),
        max_active_teams=2,
        account_pool=("acct1", "acct2"),
    )
    s.reconcile_once()
    assert rec.calls == [("pause", "team-a"), ("activate", "team-b")]


def test_desired_teams_list_takes_precedence_over_singular_shim() -> None:
    """When both fields are present (the post-task-A contract), the daemon
    uses the ranked list, not just the shim's single team."""
    s, rec = _scheduler(
        {
            "desired_teams": ["team-a", "team-b"],
            "desired_team": "team-a",
            "quiescent_teams": [],
        },
        active=(),
        max_active_teams=2,
        account_pool=("acct1", "acct2"),
    )
    s.reconcile_once()
    assert rec.activations == ["team-a", "team-b"]


def test_reorder_within_active_set_causes_no_flap(tmp_path: Path) -> None:
    """Bert: set-level anti-flap must not collapse into 'set size stable
    but members churning'. A pure reorder of the SAME top-N teams touches
    nothing."""
    clock = {"t": 1000.0}
    state = tmp_path / "state.json"

    def make(decision, active):
        return _scheduler(
            decision, installed=("team-a", "team-b"), active=active,
            dwell_minutes=20, state_path=state, now=lambda: clock["t"],
            max_active_teams=2, account_pool=("acct1", "acct2"),
        )

    s, rec = make(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []}, (),
    )
    s.reconcile_once()
    assert rec.activations == ["team-a", "team-b"]

    # 5 min later the ranking flips order, but both are still the top 2.
    clock["t"] += 5 * 60
    s2, rec2 = make(
        {"desired_teams": ["team-b", "team-a"],
         "quiescent_teams": ["team-a", "team-b"]},
        ("team-a", "team-b"),
    )
    s2.reconcile_once()
    assert rec2.calls == []  # no churn on a reorder within the active set


def test_incumbent_protected_by_its_own_dwell_on_eviction(
    tmp_path: Path,
) -> None:
    """Per-team dwell: when a new team displaces an incumbent in the
    ranking, the incumbent is not evicted until ITS OWN dwell elapses —
    and the cap means the challenger waits for a freed slot, so no flap."""
    clock = {"t": 1000.0}
    state = tmp_path / "state.json"

    def make(decision, active):
        return _scheduler(
            decision, installed=("team-a", "team-b", "team-c"),
            active=active, dwell_minutes=20, state_path=state,
            now=lambda: clock["t"], max_active_teams=2,
            account_pool=("acct1", "acct2"),
        )

    s, rec = make(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []}, (),
    )
    s.reconcile_once()
    assert rec.activations == ["team-a", "team-b"]

    # 5 min later team-c displaces team-b; team-b's dwell (15m left)
    # protects it from eviction, so the slot stays full and team-c waits.
    clock["t"] += 5 * 60
    s2, rec2 = make(
        {"desired_teams": ["team-a", "team-c"],
         "quiescent_teams": ["team-b", "team-c"]},
        ("team-a", "team-b"),
    )
    s2.reconcile_once()
    assert rec2.calls == []

    # Past team-b's dwell it is evicted and team-c takes the freed slot.
    clock["t"] += 16 * 60
    s3, rec3 = make(
        {"desired_teams": ["team-a", "team-c"],
         "quiescent_teams": ["team-b", "team-c"]},
        ("team-a", "team-b"),
    )
    s3.reconcile_once()
    assert rec3.pauses == ["team-b"]
    assert rec3.activations == ["team-c"]


# ── ADR-0092 task C: subscription-lease wiring through reconcile ─────


def test_lease_pool_binds_distinct_accounts_to_concurrent_teams() -> None:
    pool, bound = _mk_lease_pool(["acct1", "acct2"])
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        active=(), max_active_teams=2, account_pool=("acct1", "acct2"),
        lease_pool=pool,
    )
    s.reconcile_once()
    assert rec.activations == ["team-a", "team-b"]
    assert bound == {"team-a": "acct1", "team-b": "acct2"}  # distinct subs
    assert pool.leased_account("team-a") != pool.leased_account("team-b")


def test_pause_releases_the_teams_account_lease() -> None:
    pool, _ = _mk_lease_pool(["acct1", "acct2"])
    s, rec = _scheduler(
        {"desired_teams": ["team-b"], "quiescent_teams": ["team-a"]},
        installed=("team-a", "team-b"), active=("team-a",),
        max_active_teams=2, account_pool=("acct1", "acct2"), lease_pool=pool,
    )
    s.reconcile_once()
    assert rec.pauses == ["team-a"]
    assert pool.leased_account("team-a") is None  # released after pause


def test_activation_skipped_when_lease_pool_exhausted() -> None:
    """Defensive guard: a free SLOT but no free ACCOUNT -> do NOT activate
    on the shared default (the double-burn). Exercised by a lease pool with
    fewer accounts than the sizing cap."""
    pool, _ = _mk_lease_pool(["acct1"])  # only ONE real account
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        active=(), max_active_teams=2,
        account_pool=("acct1", "acct2"),  # sizing says 2 slots
        lease_pool=pool,
    )
    s.reconcile_once()
    assert rec.activations == ["team-a"]  # team-b has no free sub -> skipped
    assert pool.leased_account("team-b") is None


def test_cap_decrease_drains_without_third_activation() -> None:
    """Bert's nice-to-have: when the cap DECREASES (pool 2 -> 1) with two
    teams already active, the over-set drains via the pause phase and the
    slot ceiling prevents any new activation — a converging transient, no
    new double-burn."""
    pool, bound = _mk_lease_pool(["acct1", "acct2"])
    bound["team-a"] = "acct1"  # both live (ground truth for reconcile)
    bound["team-b"] = "acct2"
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"],
         "quiescent_teams": ["team-a", "team-b"]},
        installed=("team-a", "team-b"), active=("team-a", "team-b"),
        max_active_teams=2, account_pool=("acct1",),  # cap shrank to 1
        lease_pool=pool,
    )
    s.reconcile_once()
    assert rec.pauses == ["team-b"]   # top-1 desired is team-a; team-b drains
    assert rec.activations == []      # no third activation
    assert pool.leased_account("team-b") is None  # released on drain


# ── ADR-0092 follow-up: warm-start lease adoption ───────────────────
# Teams already running when the daemon starts never pass through the
# ACTIVATE phase (they are already active), so without warm-start they
# would keep running UNLEASED on the shared default subscription — the
# double-burn the pool exists to prevent. These pin the startup pass.


def test_warm_start_leases_already_active_unbound_teams() -> None:
    """The headline gap: two desired teams already ACTIVE with NO on-disk
    binding (started before the pool existed) get leased to distinct
    accounts on the first reconcile — no activate, no pause."""
    pool, bound = _mk_lease_pool(["acct1", "acct2"])  # empty bound = unbound
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        installed=("team-a", "team-b"), active=("team-a", "team-b"),
        max_active_teams=2, account_pool=("acct1", "acct2"), lease_pool=pool,
    )
    s.reconcile_once()
    assert rec.activations == []  # already active — nothing to activate
    assert rec.pauses == []       # both desired — nothing to pause
    assert bound == {"team-a": "acct1", "team-b": "acct2"}  # bound on warm start
    assert pool.leased_account("team-a") != pool.leased_account("team-b")


def test_warm_start_respects_rank_and_effective_max() -> None:
    """Three active unbound teams, cap=2, two accounts: only the top-2
    desired get a warm lease; the rank-3 team stays unbound (it is the one
    headed for a pause once quiescent)."""
    pool, bound = _mk_lease_pool(["acct1", "acct2"])
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b", "team-c"],
         "quiescent_teams": []},
        installed=("team-a", "team-b", "team-c"),
        active=("team-a", "team-b", "team-c"),
        max_active_teams=2, account_pool=("acct1", "acct2"), lease_pool=pool,
    )
    s.reconcile_once()
    assert bound == {"team-a": "acct1", "team-b": "acct2"}
    assert pool.leased_account("team-c") is None  # rank-3, no warm lease


def test_warm_start_does_not_disturb_reclaimed_bindings() -> None:
    """A team already bound on disk (ground truth) is reclaimed by reconcile
    and NOT re-leased; only the genuinely-unbound active team gets a warm
    lease, onto the remaining free account."""
    pool, bound = _mk_lease_pool(["acct1", "acct2"])
    bound["team-a"] = "acct2"  # team-a is live on acct2 (ground truth)
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        installed=("team-a", "team-b"), active=("team-a", "team-b"),
        max_active_teams=2, account_pool=("acct1", "acct2"), lease_pool=pool,
    )
    s.reconcile_once()
    assert pool.leased_account("team-a") == "acct2"  # reclaimed, unchanged
    assert pool.leased_account("team-b") == "acct1"  # warm-leased the free one
    assert rec.activations == [] and rec.pauses == []


def test_warm_start_pool_exhaustion_leaves_team_on_default() -> None:
    """Two active unbound teams but only ONE pool account: top-1 gets the
    lease, the rank-2 team stays unleased (on default) — logged, never
    crashes, never double-assigns."""
    pool, bound = _mk_lease_pool(["acct1"])
    s, rec = _scheduler(
        {"desired_teams": ["team-a", "team-b"], "quiescent_teams": []},
        installed=("team-a", "team-b"), active=("team-a", "team-b"),
        max_active_teams=2, account_pool=("acct1", "acct2"), lease_pool=pool,
    )
    s.reconcile_once()
    assert bound == {"team-a": "acct1"}            # only the free account bound
    assert pool.leased_account("team-b") is None   # stays on default sub
