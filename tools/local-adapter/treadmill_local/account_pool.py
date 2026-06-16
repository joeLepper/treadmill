"""ADR-0092 task bc5cdc23 — subscription-account lease pool.

When the team-scheduler runs N teams concurrently (one per Claude
subscription), each ACTIVE team must hold a DISTINCT account from a pool,
so two teams never share — and burn 2x — one subscription. This module
owns the lease assignments; the daemon (``team_scheduler.py``) is the
single writer (its host-global flock guarantees that), and binds a team's
sessions to its leased account by writing the per-label ``claude-account``
file the launcher reads (``claude-account-env.sh`` →
``CLAUDE_CONFIG_DIR=~/.claude-<account>``).

CRASH-SAFETY (the plan-review crux, Bert + Carla). Two rules make a
"running team on an account with no recorded lease" — the double-burn —
structurally impossible:

  1. LEASE PERSIST-FIRST: write+fsync the lease record BEFORE binding the
     team's ``claude-account`` files / starting its sessions. A crash
     before the team starts leaves a recorded-but-idle lease, which the
     startup reconcile safely frees as an orphan.
  2. RELEASE FREE-LAST: only clear a lease AFTER the team's sessions are
     confirmed stopped (the caller releases post-``pause``). Freeing while
     sessions still run would let a second team re-lease the account on
     top of a live one.

And on startup, RECONCILE FROM GROUND TRUTH: a running team's ACTUAL
on-disk ``claude-account`` binding is authoritative — an account a live
session is using cannot be re-leased, recorded or not. The running binding
wins over the lease file; leases for non-running teams are freed.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)


class AccountLeasePool:
    """Assigns distinct pool accounts to active teams; single-writer.

    Args:
        accounts: the pool of Claude account slugs (e.g. the two personal
            subscriptions). Order is the assignment preference.
        state_path: where lease state persists. Co-located with the
            daemon's host flock so reconcile is coherent with the lock that
            guards the single writer. ``None`` keeps it in memory (tests).
        bind_account: ``(team, account) -> None`` — writes the team's
            session labels' ``claude-account`` files. Injected for tests.
        read_bound_account: ``team -> account | None`` — the account a
            RUNNING team's sessions are actually bound to on disk (ground
            truth for reconcile). Injected for tests.
        now: injectable clock (unused today; kept for parity/future TTLs).
    """

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        state_path: Path | None,
        bind_account: Callable[[str, str], None],
        read_bound_account: Callable[[str], str | None],
    ) -> None:
        self._accounts = list(accounts)
        self._state_path = state_path
        self._bind = bind_account
        self._read_bound = read_bound_account
        self._leases: dict[str, str] = self._load()

    # ── persistence ──────────────────────────────────────────────────

    def _load(self) -> dict[str, str]:
        if self._state_path is None or not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text())
            leases = data.get("leases", {})
            if not isinstance(leases, dict):
                return {}
            return {
                str(k): str(v)
                for k, v in leases.items()
                if isinstance(v, str)
            }
        except (ValueError, OSError, TypeError, AttributeError):
            return {}

    def _persist(self) -> None:
        """Durable write: temp + fsync + atomic replace, so a crash never
        leaves a half-written lease file the next daemon would misread."""
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(
                self._state_path.suffix + ".tmp"
            )
            with open(tmp, "w") as fh:
                json.dump({"leases": self._leases}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
        except OSError:
            logger.warning(
                "could not persist account leases to %s", self._state_path,
            )

    # ── queries ──────────────────────────────────────────────────────

    def leased_account(self, team: str) -> str | None:
        return self._leases.get(team)

    def _free_accounts(self) -> list[str]:
        taken = set(self._leases.values())
        return [a for a in self._accounts if a not in taken]

    # ── lease / release ──────────────────────────────────────────────

    def lease(self, team: str) -> str | None:
        """Assign a free account to ``team`` and bind its sessions to it.
        Idempotent: a team already holding a lease keeps it. Returns the
        account, or ``None`` when the pool is exhausted (caller must NOT
        activate the team without a lease).

        Ordering is the crash-safety contract: PERSIST the lease record
        FIRST, THEN bind the ``claude-account`` files. The caller starts
        the sessions only AFTER this returns.
        """
        existing = self._leases.get(team)
        if existing is not None:
            # Re-affirm the binding (cheap, idempotent) in case a prior
            # bind was interrupted between persist and write.
            self._bind(team, existing)
            return existing
        free = self._free_accounts()
        if not free:
            logger.warning(
                "account pool exhausted — no free subscription for %s "
                "(pool=%d, leased=%d)",
                team, len(self._accounts), len(self._leases),
            )
            return None
        account = free[0]
        self._leases[team] = account
        self._persist()  # PERSIST-FIRST (before bind / activate)
        self._bind(team, account)
        logger.info("leased account %s to team %s", account, team)
        return account

    def release(self, team: str) -> None:
        """Free a team's lease. Call ONLY after the team's sessions are
        confirmed stopped (free-last)."""
        if self._leases.pop(team, None) is not None:
            self._persist()
            logger.info("released account lease for team %s", team)

    # ── startup reconcile (ground truth) ─────────────────────────────

    def reconcile(self, active_teams: Iterable[str]) -> None:
        """Rebuild lease state from GROUND TRUTH on startup: a running
        team's on-disk ``claude-account`` binding is authoritative (it
        cannot be re-leased regardless of what the lease file says), and
        leases for non-running teams are freed as orphans.

        This closes the crash window: a team that started on account X but
        crashed before/around the lease record is reclaimed from its live
        binding (X stays taken); a recorded lease whose team is not running
        is dropped (X returns to the pool).
        """
        active = set(active_teams)
        reclaimed: dict[str, str] = {}
        for team in active:
            bound = self._read_bound(team)
            if bound in self._accounts:
                reclaimed[team] = bound  # running binding wins
            elif team in self._leases and self._leases[team] in self._accounts:
                # Active, no readable binding, but a recorded lease exists —
                # keep it (the team is running; do not free a live account).
                reclaimed[team] = self._leases[team]
        dropped = set(self._leases) - set(reclaimed)
        if reclaimed != self._leases:
            self._leases = reclaimed
            self._persist()
            if dropped:
                logger.info("freed orphan account leases: %s", sorted(dropped))
