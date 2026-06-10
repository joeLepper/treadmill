"""Template installer for ``treadmill team up`` (ADR-0087 PR-E).

Copies the worker + evaluator templates from this directory into the
team's session-state tree on disk. Called from
``cli/treadmill_cli/commands/team.py`` (Bert's PR-B `treadmill team up`
implementation) once per team bootstrap.

Layout written:

    ~/.treadmill/teams/__templates__/worker/
        relay_inject_hook.py           # shared script — PostToolUse target
        settings.json.tmpl             # source (not consumed directly)
        CLAUDE.md.tmpl                 # source (not consumed directly)
    ~/.treadmill/teams/__templates__/evaluator/
        CLAUDE.md.tmpl                 # source
    ~/.treadmill/teams/__templates__/coordinator/
        CLAUDE.md.tmpl                 # source (ADR-0087 PR-D)

    ~/.treadmill/teams/<slug>/coordinator-<slug>/
        CLAUDE.md                      # rendered from template (ADR-0087 PR-D)
    ~/.treadmill/teams/<slug>/evaluator-<slug>/
        CLAUDE.md                      # rendered from template
    ~/.treadmill/teams/<slug>/worker-<slug>-N/   (one per worker)
        settings.json                  # rendered from template
        CLAUDE.md                      # rendered from template

Path coordination with Bert (PR-B):

  The plan brief (Alan's relay 2026-06-10) specifies
  ``~/.treadmill/teams/<slug>/<label>/`` as the install root. The
  existing channel-server convention is ``~/.cc-channels/<label>/``
  (holds session-id, relay/, relay-trust.json, telegram/, launcher.pid).
  Two distinct trees — channel-server state stays in ~/.cc-channels;
  team-up bootstrap state lives under ~/.treadmill/teams. PR-B writes
  the env file + systemd unit instance; PR-E (this module) writes the
  rendered CLAUDE.md + settings.json. The launcher unit reads
  TREADMILL_TEAM_TEMPLATES_DIR to find the shared scripts.

This module is stdlib-only so it can run from the freshly-installed
treadmill CLI without an editable-install + venv dance.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# Where this module's siblings (worker/, evaluator/) live in the repo.
_PACKAGE_ROOT = Path(__file__).resolve().parent

# Where rendered + shared templates land on disk.
_TEAMS_ROOT = Path.home() / ".treadmill" / "teams"
_SHARED_TEMPLATES_DIR = _TEAMS_ROOT / "__templates__"


@dataclass(frozen=True)
class TeamSpec:
    """Inputs for one team's template install."""

    repo_slug: str
    """The slug used in every label (e.g. ``medicoder``). Worker labels
    are ``worker-<slug>-1``..``-N``; coordinator is ``coordinator-<slug>``;
    evaluator is ``evaluator-<slug>``."""

    worker_count: int
    """How many worker sessions to render for. Default 3 per ADR-0087."""

    coordinator_label: str
    evaluator_label: str
    worker_labels: tuple[str, ...]


def make_team_spec(repo_slug: str, worker_count: int = 3) -> TeamSpec:
    """Derive the label set for a repo slug. Deterministic — same
    inputs always produce the same TeamSpec so re-running ``treadmill
    team up`` is idempotent."""
    return TeamSpec(
        repo_slug=repo_slug,
        worker_count=worker_count,
        coordinator_label=f"coordinator-{repo_slug}",
        evaluator_label=f"evaluator-{repo_slug}",
        worker_labels=tuple(
            f"worker-{repo_slug}-{n}" for n in range(1, worker_count + 1)
        ),
    )


def install_shared_templates(
    *, dest_root: Path = _SHARED_TEMPLATES_DIR
) -> None:
    """Copy the shared scripts + template sources to the on-disk shared
    template dir. Called once per ``treadmill team up`` invocation
    regardless of team count.

    Idempotent: overwrites existing files. Operator can hand-edit
    ``~/.treadmill/teams/__templates__/worker/CLAUDE.md.tmpl`` and a
    subsequent ``team up`` will reset it; this is the intended shape
    so the source-of-truth stays the repo's `tools/team-templates/`.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    for role in ("worker", "evaluator", "coordinator"):
        src = _PACKAGE_ROOT / role
        dst = dest_root / role
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            # AGENT.md is an editor-facing doc, not part of the
            # operator runtime tree — skip it on copy so the deployed
            # __templates__/ shape stays minimal.
            if entry.is_file() and entry.name != "AGENT.md":
                shutil.copy2(entry, dst / entry.name)


def install_team(spec: TeamSpec) -> None:
    """Render + install the per-session config files for one team.

    For each session label in the team, writes:

      ~/.treadmill/teams/<slug>/<label>/CLAUDE.md
          rendered from the corresponding role's CLAUDE.md.tmpl
      ~/.treadmill/teams/<slug>/<label>/settings.json   (workers only)
          rendered from settings.json.tmpl

    ADR-0087 PR-D extension: the coordinator's CLAUDE.md is now
    installed by this function alongside the evaluator + workers.
    The render pass uses the same shared `_render` helper; the
    coordinator template only references ``{{REPO_SLUG}}`` (no
    per-label placeholder, as there is exactly one coordinator per
    team).

    Idempotent: re-installing overwrites the rendered files. The
    operator-step to land a new template revision on a LIVE
    coordinator session is a `systemctl --user restart
    treadmill-channel@coordinator-<slug>.service` after `treadmill
    team up`; the live session reads CLAUDE.md fresh on respawn.
    """
    install_shared_templates()
    team_dir = _TEAMS_ROOT / spec.repo_slug
    team_dir.mkdir(parents=True, exist_ok=True)

    # Coordinator — rendered CLAUDE.md only (no settings.json; the
    # coordinator's session does not run with the worker's
    # PostToolUse relay-inject hook — coordinators do not run
    # subprocesses, they ARE the live process routing signals).
    coord_src = _PACKAGE_ROOT / "coordinator" / "CLAUDE.md.tmpl"
    coord_dir = team_dir / spec.coordinator_label
    coord_dir.mkdir(parents=True, exist_ok=True)
    _render(coord_src, coord_dir / "CLAUDE.md", spec)

    # Evaluator — rendered CLAUDE.md only (no settings.json; the
    # evaluator runs without the worker's PostToolUse relay-inject
    # hook).
    eval_src = _PACKAGE_ROOT / "evaluator" / "CLAUDE.md.tmpl"
    eval_dir = team_dir / spec.evaluator_label
    eval_dir.mkdir(parents=True, exist_ok=True)
    _render(eval_src, eval_dir / "CLAUDE.md", spec)

    # Workers — settings.json + CLAUDE.md per worker label.
    worker_claude_src = _PACKAGE_ROOT / "worker" / "CLAUDE.md.tmpl"
    worker_settings_src = _PACKAGE_ROOT / "worker" / "settings.json.tmpl"
    for worker_label in spec.worker_labels:
        worker_dir = team_dir / worker_label
        worker_dir.mkdir(parents=True, exist_ok=True)
        _render(worker_claude_src, worker_dir / "CLAUDE.md", spec, worker_label=worker_label)
        # settings.json doesn't carry per-worker placeholders today,
        # but copy through the same render path so a future templating
        # need (per-worker permission allowlists, e.g.) lands cleanly.
        _render(worker_settings_src, worker_dir / "settings.json", spec, worker_label=worker_label)


def _render(
    src: Path,
    dst: Path,
    spec: TeamSpec,
    *,
    worker_label: str | None = None,
) -> None:
    """Substitute ``{{REPO_SLUG}}`` and (when supplied) ``{{WORKER_LABEL}}``
    in the template body and write to ``dst``.

    Lightweight substitution by design — the templates are
    operator-readable and the placeholder set is fixed at two tokens.
    Anything more elaborate (Jinja2, etc.) would force a new dep on the
    template renderer without value.
    """
    body = src.read_text()
    body = body.replace("{{REPO_SLUG}}", spec.repo_slug)
    if worker_label is not None:
        body = body.replace("{{WORKER_LABEL}}", worker_label)
    dst.write_text(body)
    # settings.json must be 0644; CLAUDE.md likewise. The hook script
    # is 0755 by virtue of the source carrying executable bits.
    os.chmod(dst, 0o644)
