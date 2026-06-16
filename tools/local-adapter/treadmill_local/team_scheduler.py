"""Team-scheduler daemon — reconcile the active team SET.

Task 9d6c0658 (ADR-0091 finale) established the single-active daemon;
ADR-0092 (task 6c2446b2) generalized it to N concurrent active teams,
one per Claude subscription. An always-on control-plane daemon, modeled
on ``deploy_watcher.py``: on a loop it polls
``GET /api/v1/scheduler/decision`` and reconciles the running team set
toward the top ``effective_max`` of ``desired_teams`` (the API's RANKED
list) by shelling to ``treadmill-team-control`` — pause teams not in the
desired set when the API reports them quiescent, then activate desired
teams into free slots. The DECISION lives entirely in the API
(``treadmill_api/routers/scheduler.py``); this daemon enacts it and
never re-derives it.

``effective_max = min(max_active_teams, pool_size)``, but CLAMPED to 1
when no account pool is configured (ADR-0092): the subscription pool
(task bc5cdc23) is what unlocks multi-active — without it the daemon
must not run two teams on one default subscription. So at the default
(``max_active_teams=1`` / empty pool) this is byte-for-byte the ADR-0091
single active slot, and the daemon reads ``desired_teams`` with a
fallback to the singular ``desired_team`` shim so a pre-ADR-0092
decision still drives the single-slot path.

LOAD-BEARING FAIL-SAFE (Carla #342 on the plan): if the decision
endpoint is unreachable, errors, returns a malformed body, or reports
an empty desired list, the daemon HOLDS the current active set and
pauses NOTHING. The API is a SPOF (a ~9h outage occurred 2026-06-12);
the scheduler degrades to "leave things as they are", never to
"stop everything".

Reconcile contract (the four carried review notes are contractual):

1. The endpoint reports FACTS — ``quiescent_teams`` may include a
   desired team (momentarily idle between dispatches). THIS daemon
   enacts policy: it only ever pauses teams OTHER than the desired set
   (structural: the pause set is ``active - desired_set``), so a listed
   desired team is never paused.
2. Anti-flap hysteresis is PER-TEAM (ADR-0092): each team's tenure
   (activation time) is protected by a minimum dwell (default
   ``DEFAULT_DWELL_MINUTES``); a team is not evicted until ITS OWN dwell
   elapses, so a just-activated team never flaps as the ranking jitters
   below it within the set. Tenures persist so a daemon restart cannot
   flap. The dwell MUST stay <= the API's ``AGING_TIME_CONSTANT_MINUTES``
   (the aging term must never demand swaps faster than anti-flap allows
   one); the suite asserts against the imported constant directly.
3. KNOWN INHERITED EDGE (from the #344 review): quiescence predicate 3
   (half-registered PRs) only looks back
   ``HALF_REGISTERED_WINDOW_MINUTES`` (15 min). A coordinator wedged
   MID-REGISTRATION for longer than that ages out of the window and
   its team reads quiescent while a ``task_prs`` POST is still
   notionally in flight — pausing it risks the orphan-PR class. This
   daemon inherits that edge knowingly (the wedged-coordinator state
   is itself an incident; the escalation surfaces own it) rather than
   duplicating quiescence logic host-side.
4. Repo/slug casing: team slugs compared here are normalized with
   ``str.lower()`` on intake (API values AND local install-dir names),
   so a casing drift between ``team_configs.repo``-derived slugs and
   the install layout can never make an active team invisible to the
   reconciler.

Pause safety is double-walled: this daemon checks ``quiescent_teams``
before calling pause, and ``treadmill-team-control pause`` re-verifies
against the live endpoint fail-closed (#343). Resume context is the
launcher's job: ``treadmill-team-control activate`` starts the units,
the launcher resumes each label via its persisted session id
(ADR-0073), and the channel server's reconcile-on-connect emits the
``catch_up`` frame (ADR-0068) — the daemon does not need a separate
resume hook.

Single-writer: a host-global ``flock`` under ``~/.treadmill`` (the
deploy-watcher's #333 guard class — kernel-released on death, no
TOCTOU, no PID-recycling window), acquired by the daemon itself so the
exactly-one guarantee covers every spawn path.

Ships DEFAULT-OFF: ``treadmill-local up`` starts it only when
``start_team_scheduler`` is enabled (see ``runtime.py``); the operator
enables it after a manual dry-run.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from treadmill_local.account_pool import AccountLeasePool

logger = logging.getLogger("treadmill.team_scheduler")


DEFAULT_POLL_SECONDS = 60
DEFAULT_DWELL_MINUTES = 20
"""Anti-flap tenure floor. MUST stay <= the API's
``AGING_TIME_CONSTANT_MINUTES`` (30) — asserted against the imported
constant in ``tests/test_team_scheduler.py`` per the #344 review note
(the endpoint-side floor pin is slacker than the constant)."""


# ── Single-instance lock (#333 guard class) ──────────────────────────────────


def scheduler_lock_path() -> Path:
    """Host-global lock — one team-scheduler per HOST (there is one
    fleet), under ``~/.treadmill`` like the deploy-watcher's: never
    cwd-relative (per-worktree scoping is exactly the dual-instance
    bug)."""
    return Path.home() / ".treadmill" / "team-scheduler.lock"


def scheduler_pid_path() -> Path:
    return Path.home() / ".treadmill" / "team-scheduler.pid"


def scheduler_state_path() -> Path:
    """Persisted per-team dwell tenures so a daemon restart cannot reset
    the dwell window and flap."""
    return Path.home() / ".treadmill" / "team-scheduler.state"


def scheduler_account_leases_path() -> Path:
    """Persisted account-lease state (task bc5cdc23), CO-LOCATED with the
    host flock under ``~/.treadmill`` so reconcile is coherent with the
    lock that guards its single writer."""
    return Path.home() / ".treadmill" / "team-account-leases.json"


def acquire_scheduler_lock(lock_path: Path) -> int | None:
    """Try to become THE team-scheduler for this host.

    Returns the held lock fd on success (keep it open for the process
    lifetime), or ``None`` when another live scheduler holds it.
    """
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = os.read(fd, 4096).decode(errors="replace").strip()
        except OSError:
            holder = ""
        finally:
            os.close(fd)
        logger.error(
            "another team-scheduler already owns %s (%s) — refusing to "
            "start a second reconciler; two writers racing systemd is "
            "the dual-watcher class (#333)",
            lock_path, holder or "no diagnostics",
        )
        return None
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(
        fd,
        f"pid={os.getpid()} started_unix={int(time.time())}\n".encode(),
    )
    return fd


# ── The reconciler ───────────────────────────────────────────────────────────


class TeamScheduler:
    """Thin enactor of the API's scheduler decision.

    Every effectful dependency is injectable so the suite drives the
    reconcile logic with recorders and a fake clock; ``main()`` wires
    the real ones.

    Args:
        fetch_decision: returns the decoded decision dict, or ``None``
            for any failure (unreachable / HTTP error / undecodable) —
            the fail-safe HOLD path.
        team_control: ``(verb, slug) -> bool`` — shells to
            ``treadmill-team-control``; True on success.
        installed_teams: returns the slugs installed under the teams
            root (the only teams this daemon may ever touch — the
            control plane's orchestrator/operator units are structurally
            out of reach, mirroring team-control).
        team_active: ``slug -> bool`` — any of the team's units active.
        dwell_minutes: anti-flap tenure floor.
        state_path: where the last-switch stamp persists; ``None``
            keeps it in memory only (tests).
        now: injectable clock (epoch seconds).
    """

    def __init__(
        self,
        *,
        fetch_decision: Callable[[], dict | None],
        team_control: Callable[[str, str], bool],
        installed_teams: Callable[[], Iterable[str]],
        team_active: Callable[[str], bool],
        dwell_minutes: float = DEFAULT_DWELL_MINUTES,
        state_path: Path | None = None,
        now: Callable[[], float] = time.time,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        max_active_teams: int = 1,
        account_pool: Sequence[str] = (),
        lease_pool: "AccountLeasePool | None" = None,
    ) -> None:
        self._fetch_decision = fetch_decision
        self._team_control = team_control
        self._installed_teams = installed_teams
        self._team_active = team_active
        self._dwell_s = dwell_minutes * 60.0
        self._state_path = state_path
        self._now = now
        self._poll_seconds = poll_seconds
        self._max_active_teams = max(1, int(max_active_teams))
        # The account pool sizes the concurrency ceiling here (task
        # 6c2446b2); task bc5cdc23 wires the actual per-team lease/binding.
        self._account_pool = list(account_pool)
        # task bc5cdc23: when present, leases a distinct subscription per
        # active team (binding via the per-label claude-account file). None
        # = the task-B behavior (sizing only, no per-team account binding).
        self._lease_pool = lease_pool
        self._pool_reconciled = False
        self._stop_event = threading.Event()
        self._tenures: dict[str, float] = self._load_tenures()

    # ── dwell persistence (per-team tenure) ──────────────────────────
    #
    # ADR-0092: dwell is PER-TEAM, keyed by the team's activation time
    # ("tenure"), so a just-activated team is never paused while the
    # ranking jitters below it within the active set. At max_active=1
    # this reduces EXACTLY to ADR-0091's single-slot dwell (the one
    # active team's tenure gates the one possible switch) — the
    # unchanged single-active suite proves the reduction.

    def _load_tenures(self) -> dict[str, float]:
        if self._state_path is None or not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text())
            tenures = data.get("tenures", {})
            if not isinstance(tenures, dict):
                return {}
            return {
                str(k).lower(): float(v)
                for k, v in tenures.items()
                if isinstance(v, (int, float))
            }
        except (ValueError, OSError, TypeError, AttributeError):
            # An older {last_switch_unix} state file (pre-ADR-0092) has no
            # tenures key -> empty; one dwell window resets on upgrade, safe.
            return {}

    def _persist_tenures(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"tenures": self._tenures}))
        except OSError:
            logger.warning(
                "could not persist dwell tenures to %s", self._state_path,
            )

    def _stamp_tenure(self, team: str) -> None:
        self._tenures[team] = self._now()
        self._persist_tenures()

    def _clear_tenure(self, team: str) -> None:
        if self._tenures.pop(team, None) is not None:
            self._persist_tenures()

    def _dwell_remaining(self, team: str) -> float:
        stamp = self._tenures.get(team)
        if stamp is None:
            return 0.0  # unknown tenure (never-stamped / restart) = pausable
        return max(0.0, self._dwell_s - (self._now() - stamp))

    def _effective_max(self) -> int:
        """The concurrency ceiling: min(max_active_teams, pool_size), but
        CLAMPED to 1 when no account pool is configured. The pool (task
        bc5cdc23) is what unlocks multi-active — without it the daemon must
        not run two teams on the same default subscription (the double-burn).
        """
        if self._account_pool:
            return max(1, min(self._max_active_teams, len(self._account_pool)))
        return 1

    def _parse_desired_teams(
        self, decision: dict,
    ) -> tuple[list[str], bool]:
        """Read the RANKED desired list, falling back to the singular
        ``desired_team`` so a pre-task-A decision (or the unchanged
        single-active fixtures) still drive the single-slot path. Returns
        ``(ranked_lowercased_list, ok)``; ``ok=False`` means malformed → HOLD.
        """
        raw = decision.get("desired_teams")
        if raw is not None:
            if not isinstance(raw, list):
                logger.warning("malformed desired_teams %r — HOLDING", raw)
                return [], False
            return [t.lower() for t in raw if isinstance(t, str)], True
        # Back-compat: fall back to the singular shim.
        raw_one = decision.get("desired_team")
        if raw_one is None:
            return [], True
        if not isinstance(raw_one, str):
            logger.warning("malformed desired_team %r — HOLDING", raw_one)
            return [], False
        return [raw_one.lower()], True

    # ── one reconcile pass ───────────────────────────────────────────

    def reconcile_once(self) -> None:
        """Poll the decision and reconcile the active set toward the top
        ``effective_max`` desired teams.

        Every early return below is the fail-safe HOLD: no pause call
        is ever made without an affirmative, well-formed decision. At
        ``effective_max=1`` this is byte-for-byte the ADR-0091 single
        active slot.
        """
        decision = self._fetch_decision()
        if decision is None:
            logger.warning("decision unavailable — HOLDING current set")
            return

        desired_list, ok = self._parse_desired_teams(decision)
        if not ok:
            return
        raw_quiescent = decision.get("quiescent_teams")
        if not isinstance(raw_quiescent, list):
            logger.warning(
                "malformed quiescent_teams %r — HOLDING", raw_quiescent,
            )
            return
        if not desired_list:
            logger.info("no team has pending work — HOLDING current set")
            return

        # Carried note 4: normalize casing on intake, both sides.
        quiescent = {t.lower() for t in raw_quiescent if isinstance(t, str)}
        installed = {t.lower() for t in self._installed_teams()}

        # Rank-ordered desired teams that we can actually enact.
        desired_ranked = [t for t in desired_list if t in installed]
        if not desired_ranked:
            logger.warning(
                "no desired team is installed under the teams root — "
                "HOLDING (run `treadmill team up` first); desired=%r",
                desired_list,
            )
            return

        effective_max = self._effective_max()
        desired_set = set(desired_ranked[:effective_max])

        active = {t for t in installed if self._team_active(t)}
        active_now = set(active)

        # task bc5cdc23: ONE-TIME ground-truth reconcile of the lease pool
        # against the actually-running teams (a running team's on-disk
        # account binding is authoritative; orphan leases are freed). Done
        # here on the first valid decision so it has the live active set.
        if self._lease_pool is not None and not self._pool_reconciled:
            self._lease_pool.reconcile(active)
            # WARM-START LEASE ADOPTION (ADR-0092 follow-up): teams already
            # running when the daemon starts never pass through the ACTIVATE
            # phase below (they are already in active_now), so they hold no
            # lease and keep running on the shared DEFAULT subscription — the
            # double-burn the pool exists to prevent. For each desired team
            # (rank order, capped at effective_max) that is already active
            # but unleased after reconcile, claim a free account now. lease()
            # is persist-first and binds the team's claude-account files; the
            # running sessions adopt CLAUDE_CONFIG_DIR on their next restart
            # (recycle), and a later reconcile reclaims that binding as
            # ground truth. Pool-exhaustion just leaves the team on default
            # (logged) — never an activation, so no behavior regression.
            for team in desired_ranked[:effective_max]:
                if (
                    team in active
                    and self._lease_pool.leased_account(team) is None
                ):
                    if self._lease_pool.lease(team) is None:
                        logger.warning(
                            "warm-start: no free subscription for "
                            "already-active %s — it stays on the default "
                            "sub until a slot frees", team,
                        )
            self._pool_reconciled = True

        # PAUSE PHASE: drop teams not in the desired set. Never pause a
        # desired team (carried note 1: it stays in desired_set). Each pause
        # is dwell-gated by THAT team's tenure and quiescence-gated.
        for team in sorted(active - desired_set):
            remaining = self._dwell_remaining(team)
            if remaining > 0:
                logger.info(
                    "want to pause %s but its dwell has %.0fs left — holding",
                    team, remaining,
                )
                continue
            if team not in quiescent:
                logger.info(
                    "want to pause %s but it is not quiescent — "
                    "holding that pause for a later pass", team,
                )
                continue
            if self._team_control("pause", team):
                active_now.discard(team)
                self._clear_tenure(team)
                # FREE-LAST: the team's sessions are now stopped, so the
                # account can be safely returned to the pool.
                if self._lease_pool is not None:
                    self._lease_pool.release(team)
            else:
                logger.warning("pause of %s failed — will retry", team)

        # ACTIVATE PHASE: fill free slots (up to effective_max) with desired
        # teams not yet active, in rank order. Activating into a free slot
        # pauses nobody, so it is NOT dwell-gated; the new tenure is stamped
        # so the team gets dwell protection from now on. The slot ceiling is
        # what enforces the single-active invariant at effective_max=1 (a
        # still-running team that could not be paused leaves no free slot).
        for team in desired_ranked:
            if team in active_now:
                continue
            if len(active_now) >= effective_max:
                break
            # task bc5cdc23: LEASE a distinct subscription BEFORE activating
            # (the pool persists the lease record + binds the team's
            # claude-account files first — persist-first crash-safety). No
            # free account => do NOT activate on the shared default (the
            # double-burn); log and wait for a freed slot.
            if self._lease_pool is not None:
                if self._lease_pool.lease(team) is None:
                    logger.warning(
                        "no free subscription to activate %s — waiting", team,
                    )
                    continue
            logger.info("activating %s", team)
            if self._team_control("activate", team):
                active_now.add(team)
                self._stamp_tenure(team)
            elif self._lease_pool is not None:
                # Activation failed after leasing — return the account so it
                # is not stranded; a later pass re-leases and retries.
                self._lease_pool.release(team)

    # ── loop ─────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info(
            "team-scheduler running (poll=%ss, dwell=%smin)",
            self._poll_seconds, self._dwell_s / 60.0,
        )
        while not self._stop_event.is_set():
            try:
                self.reconcile_once()
            except Exception:
                # The loop must survive any single pass; holding is
                # always safe.
                logger.exception("reconcile pass failed — HOLDING")
            self._stop_event.wait(self._poll_seconds)


# ── real-world wiring ────────────────────────────────────────────────────────


def default_team_control_path() -> Path:
    """The in-repo location of ``treadmill-team-control``, resolved from
    this module's own path: ``tools/local-adapter/treadmill_local/`` →
    ``parents[2]`` is ``tools/`` → ``tools/cc-channels/systemd/``.

    Task 1518598a regression note: the original wiring used
    ``parents[3]`` (the REPO ROOT), which resolves to
    ``<repo>/cc-channels/systemd`` — missing the ``tools/`` segment —
    so the daemon exited 1 unless ``TREADMILL_TEAM_CONTROL`` overrode
    it. The plan's coarse grep gate never stat'd the path; the suite
    now asserts this function resolves to an EXISTING file on the real
    tree.
    """
    return (
        Path(__file__).resolve().parents[2]
        / "cc-channels" / "systemd" / "treadmill-team-control"
    )


def _real_fetch_decision(api_url: str) -> dict | None:
    try:
        with urllib.request.urlopen(
            api_url.rstrip("/") + "/api/v1/scheduler/decision", timeout=10,
        ) as resp:
            body = resp.read()
        decoded = json.loads(body)
        return decoded if isinstance(decoded, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _team_label_dirs(teams_root: Path, slug: str) -> list[str]:
    """The team's labels from the install layout — the same three
    shapes ``treadmill-team-control`` accepts; anything else under the
    team dir is ignored. Orchestrator/operator labels live outside the
    teams root and can never appear."""
    labels: list[str] = []
    team_dir = teams_root / slug
    if not team_dir.is_dir():
        return labels
    for child in sorted(team_dir.iterdir()):
        if not child.is_dir():
            continue
        base = child.name
        if base in (f"coordinator-{slug}", f"evaluator-{slug}"):
            labels.append(base)
        elif base.startswith(f"worker-{slug}-"):
            suffix = base[len(f"worker-{slug}-"):]
            if suffix.isdigit():
                labels.append(base)
    return labels


def _real_installed_teams(teams_root: Path) -> set[str]:
    if not teams_root.is_dir():
        return set()
    return {
        d.name for d in teams_root.iterdir()
        if d.is_dir() and _team_label_dirs(teams_root, d.name)
    }


def _real_team_active(teams_root: Path, slug: str) -> bool:
    """A team counts active when ANY of its label units is active."""
    for label in _team_label_dirs(teams_root, slug):
        result = subprocess.run(
            [
                "systemctl", "--user", "is-active", "--quiet",
                f"treadmill-channel@{label}.service",
            ],
            check=False,
        )
        if result.returncode == 0:
            return True
    return False


def _cc_channels_account_file(label: str) -> Path:
    """The per-label ``claude-account`` file the launcher reads
    (``claude-account-env.sh``: ``STATE_ROOT=~/.cc-channels/<label>``)."""
    return Path.home() / ".cc-channels" / label / "claude-account"


def _real_bind_account(teams_root: Path, slug: str, account: str) -> None:
    """Bind every session label of the team to ``account`` by writing its
    ``claude-account`` file — the launcher then sets
    ``CLAUDE_CONFIG_DIR=~/.claude-<account>`` for that session."""
    for label in _team_label_dirs(teams_root, slug):
        path = _cc_channels_account_file(label)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(account + "\n")
        except OSError:
            logger.warning(
                "could not write claude-account for %s -> %s", label, account,
            )


def _real_read_bound_account(teams_root: Path, slug: str) -> str | None:
    """Ground truth for reconcile: the account a running team's sessions
    are actually bound to, read from its first label's ``claude-account``
    file (all labels of a team share one leased account)."""
    for label in _team_label_dirs(teams_root, slug):
        path = _cc_channels_account_file(label)
        try:
            value = path.read_text().strip()
            if value:
                return value
        except OSError:
            continue
    return None


def _real_team_control(script: Path, verb: str, slug: str) -> bool:
    result = subprocess.run(
        [str(script), verb, slug], check=False,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning(
            "treadmill-team-control %s %s failed (rc=%s): %s",
            verb, slug, result.returncode, result.stderr.strip()[-500:],
        )
        return False
    return True


def main() -> int:
    """Production entrypoint: flock, pidfile, real callables, loop.

    Env:
      TREADMILL_API_URL                  decision endpoint base
                                         (default http://localhost:8088)
      TREADMILL_TEAMS_ROOT               default ~/.treadmill/teams
      TREADMILL_TEAM_CONTROL             path to treadmill-team-control
                                         (default: resolved next to the
                                         repo's tools/cc-channels/systemd)
      TREADMILL_TEAM_SCHEDULER_DWELL_MINUTES   default 20
      TREADMILL_TEAM_SCHEDULER_POLL_SECONDS    default 60
      TREADMILL_TEAM_SCHEDULER_LOG_FILE        rotating log target
      TREADMILL_MAX_ACTIVE_TEAMS               max concurrent teams
                                               (ADR-0092; default 1)
      TREADMILL_TEAM_ACCOUNT_POOL              comma-separated Claude
                                               account slugs; sizes the
                                               concurrency cap. Empty =>
                                               clamp to 1 (no double-burn).
    """
    from treadmill_local.runtime import (
        STATE_DIR,
        TEAM_SCHEDULER_LOG_FILE,
        TEAM_SCHEDULER_PID_FILE,
    )
    from treadmill_local.subprocess_logging import configure_rotating_logging

    log_file_env = os.environ.get("TREADMILL_TEAM_SCHEDULER_LOG_FILE")
    log_file = Path(log_file_env) if log_file_env else TEAM_SCHEDULER_LOG_FILE
    configure_rotating_logging(log_file)

    # Lock FIRST, before any side effect (#333 discipline): a losing
    # instance must leave the winner's state untouched. Refusal is
    # SUCCESS for the invariant — exit 0.
    lock_fd = acquire_scheduler_lock(scheduler_lock_path())
    if lock_fd is None:
        return 0
    _ = lock_fd  # held for process lifetime

    scheduler_pid_path().write_text(str(os.getpid()))
    STATE_DIR.mkdir(exist_ok=True)
    TEAM_SCHEDULER_PID_FILE.write_text(str(os.getpid()))

    api_url = os.environ.get("TREADMILL_API_URL", "http://localhost:8088")
    teams_root = Path(
        os.environ.get(
            "TREADMILL_TEAMS_ROOT", str(Path.home() / ".treadmill" / "teams"),
        )
    )
    control_env = os.environ.get("TREADMILL_TEAM_CONTROL")
    if control_env:
        control_script = Path(control_env)
    else:
        control_script = default_team_control_path()
    if not control_script.exists():
        logger.error(
            "treadmill-team-control not found at %s — set "
            "TREADMILL_TEAM_CONTROL", control_script,
        )
        return 1

    def _env_float(name: str, default: float) -> float:
        try:
            v = float(os.environ.get(name, ""))
            return v if v > 0 else default
        except ValueError:
            return default

    dwell = _env_float(
        "TREADMILL_TEAM_SCHEDULER_DWELL_MINUTES", DEFAULT_DWELL_MINUTES,
    )
    poll = _env_float(
        "TREADMILL_TEAM_SCHEDULER_POLL_SECONDS", DEFAULT_POLL_SECONDS,
    )

    # ADR-0092 multi-active: how many teams may run at once, and the pool of
    # Claude subscription accounts to size that against. DEFAULT 1 + empty
    # pool => byte-for-byte ADR-0091 single-active. The no-pool clamp lives
    # in _effective_max (a max>1 set without a pool stays capped at 1).
    try:
        max_active = int(os.environ.get("TREADMILL_MAX_ACTIVE_TEAMS", "1"))
    except ValueError:
        max_active = 1
    account_pool = [
        a.strip()
        for a in os.environ.get("TREADMILL_TEAM_ACCOUNT_POOL", "").split(",")
        if a.strip()
    ]

    # task bc5cdc23: the lease pool that binds each active team to a distinct
    # subscription. Only when a pool is configured (otherwise single-active
    # on the default account, no leasing — the task-B behavior).
    lease_pool = (
        AccountLeasePool(
            accounts=account_pool,
            state_path=scheduler_account_leases_path(),
            bind_account=lambda slug, account: _real_bind_account(
                teams_root, slug, account,
            ),
            read_bound_account=lambda slug: _real_read_bound_account(
                teams_root, slug,
            ),
        )
        if account_pool
        else None
    )

    scheduler = TeamScheduler(
        fetch_decision=lambda: _real_fetch_decision(api_url),
        team_control=lambda verb, slug: _real_team_control(
            control_script, verb, slug,
        ),
        installed_teams=lambda: _real_installed_teams(teams_root),
        team_active=lambda slug: _real_team_active(teams_root, slug),
        dwell_minutes=dwell,
        state_path=scheduler_state_path(),
        poll_seconds=poll,
        max_active_teams=max_active,
        account_pool=account_pool,
        lease_pool=lease_pool,
    )

    import signal as _signal

    def _terminate(_signum: int, _frame: object) -> None:
        scheduler.stop()

    _signal.signal(_signal.SIGTERM, _terminate)
    _signal.signal(_signal.SIGINT, _terminate)

    scheduler.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
