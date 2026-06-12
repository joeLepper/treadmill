"""Tests for the usage-limit-park detection + recovery (task b561910d).

The 2026-06-11 class: the interactive limit modal blocks claude on stdin
— no exit, no relay — so the coordinator sees 'executing' with zero
output for hours. Detection reads the SESSION SURFACE (tmux pane);
recovery fails over to a pre-configured account or escalates with the
manual recipe. The platform NEVER auto-selects the billing options.

Harness: fake HOME; tmux stubbed to serve pane content from a fixture
file; curl stubbed to record event POSTs.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

SYSTEMD_DIR = Path(__file__).resolve().parents[1] / "systemd"
CHECK = SYSTEMD_DIR / "treadmill-limit-park-check"
RECOVER = SYSTEMD_DIR / "treadmill-limit-park-recover"
WRAPPER = SYSTEMD_DIR / "treadmill-channel-launch"
ACCOUNT_ENV = Path(__file__).resolve().parents[1] / "claude-account-env.sh"
LABEL = "worker-limitteam-2"

MODAL = """\
 You've reached your usage limit.
   1. Stop and wait for limit to reset (resets 3am)
   2. Switch to usage credits
   3. Switch to Team plan
"""


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    (home / ".cc-channels" / LABEL).mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(exist_ok=True)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text("")
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = "capture-pane" ]; then cat "{pane_file}"; '
        f'else echo "$@" >> "{tmp_path}/tmux.log"; fi\n'
    )
    tmux.chmod(tmux.stat().st_mode | stat.S_IEXEC)
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'while [ $# -gt 0 ]; do [ "$1" = "-d" ] && { shift; '
        f'echo "$1" >> "{tmp_path}/curl.log"; }}; shift; done\n'
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }


def _state(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".cc-channels" / LABEL


def _set_pane(tmp_path: Path, content: str) -> None:
    (tmp_path / "pane.txt").write_text(content)


def _check(env: dict[str, str]) -> int:
    return subprocess.run(
        [str(CHECK), LABEL], env=env, capture_output=True, timeout=10,
    ).returncode


def _recover(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RECOVER), LABEL], env=env, capture_output=True, text=True, timeout=10,
    )


def _events(tmp_path: Path) -> list[dict]:
    log = tmp_path / "curl.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


# ── Detection ────────────────────────────────────────────────────────


def test_frozen_modal_confirms_on_second_check(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _set_pane(tmp_path, MODAL)
    assert _check(env) == 1  # first sighting: record, don't fire
    assert _check(env) == 0  # frozen + signature, twice: parked


def test_busy_worker_mentioning_signature_never_fires(tmp_path: Path) -> None:
    """The false-positive guard: a live worker DISCUSSING the limit
    prompt keeps redrawing — pane hash changes every beat."""
    env = _env(tmp_path)
    for i in range(4):
        _set_pane(tmp_path, f"grep 'limit to reset' docs/learning.md  # beat {i}\n")
        assert _check(env) == 1


def test_no_signature_clears_state(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _set_pane(tmp_path, MODAL)
    assert _check(env) == 1
    _set_pane(tmp_path, "normal busy output\n")
    assert _check(env) == 1
    assert not (_state(env) / "limit-park.state").exists()
    # Signature returning later starts the confirmation over.
    _set_pane(tmp_path, MODAL)
    assert _check(env) == 1
    assert _check(env) == 0


# ── Recovery: failover ───────────────────────────────────────────────


def test_failover_swaps_account_and_requests_bounce(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (Path(env["HOME"]) / ".claude-fallback1").mkdir()
    (_state(env) / "claude-account-fallback").write_text("fallback1\n")
    (_state(env) / "claude-account").write_text("primary\n")

    result = _recover(env)

    assert result.returncode == 0, result.stderr  # 0 = bounce wanted
    assert (_state(env) / "claude-account").read_text().strip() == "fallback1"
    assert (_state(env) / "claude-account.limited").read_text().strip() == "primary"
    (event,) = _events(tmp_path)
    assert event["action"] == "worker_limit_parked"
    assert event["payload"]["recovery"] == "failover"
    assert event["payload"]["fallback_account"] == "fallback1"


def test_fallback_named_but_dir_missing_escalates(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (_state(env) / "claude-account-fallback").write_text("ghost\n")

    result = _recover(env)

    assert result.returncode == 2
    (event,) = _events(tmp_path)
    assert event["payload"]["recovery"] == "escalate"


# ── Recovery: escalation ─────────────────────────────────────────────


def test_escalation_emits_event_and_coordinator_relay(tmp_path: Path) -> None:
    env = _env(tmp_path)
    relay_dir = Path(env["HOME"]) / ".cc-channels" / "coordinator-limitteam" / "relay"
    relay_dir.mkdir(parents=True)

    result = _recover(env)

    assert result.returncode == 2
    (event,) = _events(tmp_path)
    assert event["action"] == "worker_limit_parked"
    assert event["payload"]["recovery"] == "escalate"
    # The recipe travels in the event AND the relay.
    assert "send-keys" in event["payload"]["recipe"]
    relay_files = list(relay_dir.glob("*limit-park*"))
    assert len(relay_files) == 1
    body = relay_files[0].read_text()
    assert f"[from: {LABEL}]" in body
    assert "NEVER select the billing options" in body


def test_escalation_is_rate_limited(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _recover(env).returncode == 2
    assert _recover(env).returncode == 2  # second call within 30 min
    assert len(_events(tmp_path)) == 1  # but only ONE event emitted


# ── Account selection (launch-session prerequisite) ──────────────────


def _resolve_config_dir(tmp_path: Path, account: str | None) -> str:
    home = tmp_path / "home"
    state = home / ".cc-channels" / LABEL
    state.mkdir(parents=True, exist_ok=True)
    if account is not None:
        (state / "claude-account").write_text(account + "\n")
    result = subprocess.run(
        [
            "bash", "-c",
            f'STATE_ROOT="{state}" LABEL="{LABEL}" '
            f'source "{ACCOUNT_ENV}" && echo "${{CLAUDE_CONFIG_DIR:-default}}"',
        ],
        env={**os.environ, "HOME": str(home)},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_account_file_selects_config_dir(tmp_path: Path) -> None:
    (tmp_path / "home" / ".claude-acct2").mkdir(parents=True)
    assert _resolve_config_dir(tmp_path, "acct2") == str(
        tmp_path / "home" / ".claude-acct2"
    )


def test_no_account_file_keeps_default(tmp_path: Path) -> None:
    assert _resolve_config_dir(tmp_path, None) == "default"


def test_missing_account_dir_warns_and_keeps_default(tmp_path: Path) -> None:
    assert _resolve_config_dir(tmp_path, "nonexistent") == "default"


# ── Billing-safety + wiring pins ─────────────────────────────────────


def test_wrapper_limit_path_sends_only_enter() -> None:
    """HARD CONSTRAINT pin: in the wrapper's limit-modal branch the only
    key ever sent is Enter — no '2', no '3', no arrows (billing options
    are operator-only)."""
    body = WRAPPER.read_text()
    start = body.index('*"limit to reset"*)')
    end = body.index(";;", start)
    branch = body[start:end]
    assert 'send-keys -t "$LABEL" Enter' in branch
    import re
    sends = re.findall(r"send-keys[^\n]*", branch)
    assert sends == [f'send-keys -t "$LABEL" Enter']


def test_recover_script_never_touches_tmux_keys() -> None:
    """The recover script carries the manual recipe as PROSE (it names
    send-keys for the human) but never INVOKES tmux itself."""
    for line in RECOVER.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("RECIPE="):
            continue
        assert not stripped.startswith("tmux"), line
        for sep in ("&& tmux", "; tmux", "| tmux", "$(tmux"):
            assert sep not in stripped, line


def test_wrapper_wires_check_and_recover_in_keepalive_loop() -> None:
    body = WRAPPER.read_text()
    assert "treadmill-limit-park-check" in body
    assert "treadmill-limit-park-recover" in body
    # Failover bounces the unit (exit 1 -> Restart=on-failure + #326 reap).
    loop = body[body.index("while tmux has-session"):]
    assert "exit 1" in loop


def test_launcher_sources_account_env() -> None:
    body = (Path(__file__).resolve().parents[1] / "launch-session.sh").read_text()
    assert "claude-account-env.sh" in body
    # Sourced AFTER pidfile write, immediately before exec claude.
    assert body.index("claude-account-env.sh") < body.index("exec claude")
