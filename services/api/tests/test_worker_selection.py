"""Pure (DB-free) foils for the ADR-0118 worker-assignment core ``select_worker``.

``select_worker`` is the pure decision the dispatch consumer runs to pick a worker: continuity
for a rework, else load-balance over the roster. The DB wiring (roster from
``team_configs.worker_labels``, in-flight load, prior-worker lookup) is covered by the real-DB
foils in ``test_integration_dispatch_consumer.py``; this file gates the policy itself, no DB.
"""

from __future__ import annotations

from treadmill_api.coordination.dispatch_consumer import select_worker

ROSTER = ["worker-r-1", "worker-r-2", "worker-r-3"]


def test_continuity_reuses_prior_even_when_more_loaded():
    # a rework must return to the worker with context, regardless of its load.
    assert select_worker(ROSTER, {"worker-r-1": 9, "worker-r-2": 0}, "worker-r-1") == "worker-r-1"


def test_prior_not_in_roster_falls_back_to_load_balance():
    # the prior worker was retired (roster shrank) → pick the least-loaded current worker.
    got = select_worker(ROSTER, {"worker-r-1": 2, "worker-r-2": 0, "worker-r-3": 1}, "gone")
    assert got == "worker-r-2"


def test_no_prior_picks_least_loaded():
    got = select_worker(ROSTER, {"worker-r-1": 1, "worker-r-2": 1, "worker-r-3": 0}, None)
    assert got == "worker-r-3"


def test_tie_breaks_by_roster_order():
    # all equal (empty load map) → the first roster entry, deterministically.
    assert select_worker(ROSTER, {}, None) == "worker-r-1"


def test_partial_load_map_treats_absent_as_zero():
    # worker-r-2 absent from the map → load 0 → chosen over the loaded worker-r-1.
    assert select_worker(ROSTER, {"worker-r-1": 3}, None) == "worker-r-2"


def test_empty_roster_returns_none():
    assert select_worker([], {}, None) is None
    # even with a prior: no roster means the caller must use its synthetic fallback.
    assert select_worker([], {}, "worker-r-1") is None
