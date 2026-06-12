"""ADR-0091 team-scheduler daemon — reconcile the single active team.

Task 9d6c0658 (plan 992d65b7, the ADR-0091 finale). An always-on
control-plane daemon, modeled on ``deploy_watcher.py``: on a loop it
polls ``GET /api/v1/scheduler/decision`` and reconciles the running
team set toward ``desired_team`` by shelling to
``treadmill-team-control`` — pause the current team only when the API
reports it quiescent, then activate the desired team. The DECISION
lives entirely in the API (``treadmill_api/routers/scheduler.py``);
this daemon enacts it and never re-derives it.

LOAD-BEARING FAIL-SAFE (Carla #342 on the plan): if the decision
endpoint is unreachable, errors, returns a malformed body, or reports
``desired_team: null``, the daemon HOLDS the current active set and
pauses NOTHING. The API is a SPOF (a ~9h outage occurred 2026-06-12);
the scheduler degrades to "leave things as they are", never to
"stop everything".

Reconcile contract (the four carried review notes are contractual):

1. The endpoint reports FACTS — ``quiescent_teams`` may include
   ``desired_team`` (momentarily idle between dispatches). THIS daemon
   enacts policy: it only ever pauses teams OTHER than the desired one
   (structural: the pause set is ``active - {desired}``), so a listed
   desired team is never paused.
2. Anti-flap hysteresis: a team's tenure is protected by a minimum
   dwell (default ``DEFAULT_DWELL_MINUTES``); at most one switch per
   window, stamped persistently so a daemon restart cannot flap. The
   dwell MUST stay <= the API's ``AGING_TIME_CONSTANT_MINUTES`` (the
   aging term must never demand swaps faster than anti-flap allows
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
from collections.abc import Callable, Iterable
from pathlib import Path

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
    """Persisted last-switch stamp so a daemon restart cannot reset the
    dwell window and flap."""
    return Path.home() / ".treadmill" / "team-scheduler.state"


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
    ) -> None:
        self._fetch_decision = fetch_decision
        self._team_control = team_control
        self._installed_teams = installed_teams
        self._team_active = team_active
        self._dwell_s = dwell_minutes * 60.0
        self._state_path = state_path
        self._now = now
        self._poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._last_switch: float | None = self._load_last_switch()

    # ── dwell persistence ────────────────────────────────────────────

    def _load_last_switch(self) -> float | None:
        if self._state_path is None or not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text())
            return float(data["last_switch_unix"])
        except (ValueError, KeyError, OSError, TypeError):
            return None

    def _stamp_switch(self) -> None:
        self._last_switch = self._now()
        if self._state_path is not None:
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                self._state_path.write_text(
                    json.dumps({"last_switch_unix": self._last_switch})
                )
            except OSError:
                logger.warning(
                    "could not persist dwell stamp to %s", self._state_path,
                )

    def _dwell_remaining(self) -> float:
        if self._last_switch is None:
            return 0.0
        return max(0.0, self._dwell_s - (self._now() - self._last_switch))

    # ── one reconcile pass ───────────────────────────────────────────

    def reconcile_once(self) -> None:
        """Poll the decision and take at most one switch toward it.

        Every early return below is the fail-safe HOLD: no pause call
        is ever made without an affirmative, well-formed decision.
        """
        decision = self._fetch_decision()
        if decision is None:
            logger.warning("decision unavailable — HOLDING current set")
            return

        raw_desired = decision.get("desired_team")
        raw_quiescent = decision.get("quiescent_teams")
        if raw_desired is not None and not isinstance(raw_desired, str):
            logger.warning("malformed desired_team %r — HOLDING", raw_desired)
            return
        if not isinstance(raw_quiescent, list):
            logger.warning(
                "malformed quiescent_teams %r — HOLDING", raw_quiescent,
            )
            return
        if raw_desired is None:
            logger.info("no team has pending work — HOLDING current set")
            return

        # Carried note 4: normalize casing on intake, both sides.
        desired = raw_desired.lower()
        quiescent = {
            t.lower() for t in raw_quiescent if isinstance(t, str)
        }
        installed = {t.lower() for t in self._installed_teams()}

        if desired not in installed:
            logger.warning(
                "desired team %r is not installed under the teams root — "
                "HOLDING (run `treadmill team up` for it first)", desired,
            )
            return

        active = {t for t in installed if self._team_active(t)}
        others = active - {desired}  # carried note 1: never pause desired

        if not others:
            if desired in active:
                return  # steady state
            # Nothing else is running — activating into an idle fleet
            # pauses nobody, so it is not dwell-gated; the new tenure
            # IS stamped so it gets dwell protection from now on.
            logger.info("activating %s (no other team active)", desired)
            if self._team_control("activate", desired):
                self._stamp_switch()
            return

        # A switch (pausing a current team) is dwell-gated.
        remaining = self._dwell_remaining()
        if remaining > 0:
            logger.info(
                "switch to %s wanted but dwell has %.0fs left — HOLDING",
                desired, remaining,
            )
            return

        all_paused = True
        for team in sorted(others):
            if team not in quiescent:
                logger.info(
                    "switch to %s wanted but %s is not quiescent — "
                    "holding that pause for a later pass",
                    desired, team,
                )
                all_paused = False
                continue
            if not self._team_control("pause", team):
                logger.warning("pause of %s failed — will retry", team)
                all_paused = False

        if not all_paused:
            # Single-active invariant over progress: do not activate
            # the desired team while another team is still running.
            return

        self._stamp_switch()
        logger.info("activating %s", desired)
        self._team_control("activate", desired)

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
