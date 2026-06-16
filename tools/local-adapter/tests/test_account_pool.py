"""Tests for the ADR-0092 subscription-account lease pool (task bc5cdc23).

The crux (Bert + Carla on the plan): a "running team on an account with no
recorded lease" — the double-burn — must be structurally impossible.
Pinned here:
  * LEASE PERSIST-FIRST: the lease record is on disk BEFORE the bind runs.
  * distinct accounts per team; idempotent re-lease; exhaustion -> None.
  * RELEASE frees the account for re-lease.
  * RECONCILE from GROUND TRUTH: a running team's on-disk binding wins
    over the lease file; orphan leases (team not running) are freed; a
    running team with NO recorded lease keeps its account (not reassigned).
"""

from __future__ import annotations

import json
from pathlib import Path

from treadmill_local.account_pool import AccountLeasePool


class FakeBindings:
    """Stands in for the on-disk per-label ``claude-account`` files."""

    def __init__(self) -> None:
        self.bound: dict[str, str] = {}
        self.bind_calls: list[tuple[str, str]] = []

    def bind(self, team: str, account: str) -> None:
        self.bind_calls.append((team, account))
        self.bound[team] = account

    def read(self, team: str) -> str | None:
        return self.bound.get(team)


def _pool(accounts, *, state_path=None, bindings: FakeBindings | None = None):
    bindings = bindings or FakeBindings()
    pool = AccountLeasePool(
        accounts=accounts,
        state_path=state_path,
        bind_account=bindings.bind,
        read_bound_account=bindings.read,
    )
    return pool, bindings


# ── distinct assignment, idempotence, exhaustion ─────────────────────


def test_lease_assigns_distinct_accounts() -> None:
    pool, b = _pool(["acct1", "acct2"])
    assert pool.lease("team-a") == "acct1"
    assert pool.lease("team-b") == "acct2"  # distinct
    assert b.bound == {"team-a": "acct1", "team-b": "acct2"}


def test_lease_is_idempotent_per_team() -> None:
    pool, _ = _pool(["acct1", "acct2"])
    assert pool.lease("team-a") == "acct1"
    assert pool.lease("team-a") == "acct1"  # same account, not acct2


def test_exhausted_pool_returns_none() -> None:
    pool, _ = _pool(["only-one"])
    assert pool.lease("team-a") == "only-one"
    assert pool.lease("team-b") is None  # no free account


def test_release_frees_account_for_release() -> None:
    pool, _ = _pool(["acct1", "acct2"])
    pool.lease("team-a")
    pool.lease("team-b")
    assert pool.lease("team-c") is None  # exhausted
    pool.release("team-a")
    assert pool.lease("team-c") == "acct1"  # freed account reused


# ── the crash-safety crux ────────────────────────────────────────────


def test_lease_persists_record_before_binding(tmp_path: Path) -> None:
    """PERSIST-FIRST: at the moment bind runs, the lease record is ALREADY
    durably on disk — so a crash during/after bind leaves a recorded lease
    (safe, reclaimable), never a bound-but-unrecorded account."""
    state = tmp_path / "leases.json"
    seen: dict[str, str | None] = {}

    def bind(team: str, account: str) -> None:
        on_disk = json.loads(state.read_text())["leases"]
        seen["at_bind"] = on_disk.get(team)

    pool = AccountLeasePool(
        accounts=["acct1"], state_path=state,
        bind_account=bind, read_bound_account=lambda t: None,
    )
    assert pool.lease("team-a") == "acct1"
    assert seen["at_bind"] == "acct1"  # persisted BEFORE bind ran


def test_leases_persist_across_restart(tmp_path: Path) -> None:
    state = tmp_path / "leases.json"
    pool, _ = _pool(["acct1", "acct2"], state_path=state)
    pool.lease("team-a")
    # "restart": a fresh pool reads the same state file.
    pool2, _ = _pool(["acct1", "acct2"], state_path=state)
    assert pool2.leased_account("team-a") == "acct1"
    assert pool2.lease("team-b") == "acct2"  # acct1 still taken


# ── ground-truth reconcile ───────────────────────────────────────────


def test_reconcile_frees_orphan_lease(tmp_path: Path) -> None:
    """Crash-after-persist-before-start: a lease was recorded but the team
    never came up. On restart it is not active -> the lease is freed."""
    state = tmp_path / "leases.json"
    pool, b = _pool(["acct1", "acct2"], state_path=state)
    pool.lease("team-a")  # recorded, but team-a never started
    # restart: team-a is NOT active.
    pool2, _ = _pool(["acct1", "acct2"], state_path=state, bindings=b)
    pool2.reconcile(active_teams=[])  # nothing running
    assert pool2.leased_account("team-a") is None  # orphan freed
    assert pool2.lease("team-b") == "acct1"  # acct1 back in the pool


def test_reconcile_reclaims_running_team_with_no_recorded_lease(
    tmp_path: Path,
) -> None:
    """The double-burn guard: a team is RUNNING bound to acct1 on disk but
    has NO recorded lease (e.g. crash before the record, defensive). The
    running binding is authoritative — reconcile reclaims acct1 so a second
    team can NEVER be handed it."""
    state = tmp_path / "leases.json"
    b = FakeBindings()
    b.bound["team-a"] = "acct1"  # team-a is live on acct1 (on-disk binding)
    pool, _ = _pool(["acct1", "acct2"], state_path=state, bindings=b)
    assert pool.leased_account("team-a") is None  # no recorded lease yet

    pool.reconcile(active_teams=["team-a"])
    assert pool.leased_account("team-a") == "acct1"  # reclaimed from disk
    # acct1 must NOT be handed to another team.
    assert pool.lease("team-b") == "acct2"


def test_reconcile_running_binding_wins_over_stale_lease(
    tmp_path: Path,
) -> None:
    """If the lease file disagrees with the live on-disk binding, the
    RUNNING binding wins (it is what the sessions actually use)."""
    state = tmp_path / "leases.json"
    b = FakeBindings()
    pool, _ = _pool(["acct1", "acct2"], state_path=state, bindings=b)
    pool.lease("team-a")  # records team-a -> acct1
    # But the team actually came up bound to acct2 on disk (skew).
    b.bound["team-a"] = "acct2"
    pool.reconcile(active_teams=["team-a"])
    assert pool.leased_account("team-a") == "acct2"  # ground truth wins
    # acct1 is freed; acct2 is taken.
    assert pool.lease("team-b") == "acct1"
