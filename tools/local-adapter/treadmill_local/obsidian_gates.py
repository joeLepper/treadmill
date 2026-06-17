"""Obsidian-sync gate implementations (ADR-0078 §4).

This module ships the secret-leak gate at v1 — the load-bearing one
because the Treadmill repo is PUBLIC. Other gates (filename validity,
ADR-immutability, no-source, creation-disallowed) ship with the
conform-write task in a sibling PR.

The gate framework (Gate protocol + GateContext + GateResult) lives
in ``obsidian_sync.py``. Gates here implement that protocol.

Secret-leak baseline — OUT OF SOURCE CONTROL
--------------------------------------------
The baseline denylist (real client names + the deployment account-id)
is NOT hardcoded here — that would leak the very literals the gate
exists to block into the PUBLIC repo. Instead it is loaded at runtime
from an operator-local file outside the repo tree
(``~/.treadmill/codenames.json`` by default, override via
``TREADMILL_CODENAMES_FILE``). Public source carries only the loader +
the path. A clone without that file degrades to an empty baseline —
correct, because a public contributor has no client secrets to leak;
``RepoConfig.sensitive_strings`` still layers per-repo extras on top.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from treadmill_local.obsidian_sync import GateContext, GateResult

logger = logging.getLogger("treadmill_local.obsidian_gates")


# ── Out-of-source-control sensitive-string baseline ─────────────────────


_DEFAULT_CODENAMES_PATH = Path.home() / ".treadmill" / "codenames.json"


def _codenames_path() -> Path:
    """Resolve the operator-local codename/denylist file path.

    ``TREADMILL_CODENAMES_FILE`` overrides the default. The file lives
    OUTSIDE the repo tree so it can never be ``git add``-ed.
    """
    override = os.environ.get("TREADMILL_CODENAMES_FILE")
    return Path(override) if override else _DEFAULT_CODENAMES_PATH


def load_baseline_sensitive_strings() -> tuple[str, ...]:
    """Load the public-repo baseline denylist from the out-of-SC file.

    Returns the ``denylist`` array from ``codenames.json`` as a tuple.
    Missing file → empty tuple (normal for a public clone; logged at
    debug, not warning). A present-but-unparseable file → empty tuple +
    a warning (a real misconfiguration the operator should see), never
    a raise: a gate that crashes the sync daemon is worse than a gate
    that no-ops with a loud log (the per-repo extras still apply).
    """
    path = _codenames_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug(
            "codenames file %s absent — empty secret-leak baseline "
            "(expected on a public clone; per-repo extras still apply)",
            path,
        )
        return ()
    except OSError as exc:
        logger.warning("could not read codenames file %s: %s", path, exc)
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("codenames file %s is unparseable: %s", path, exc)
        return ()
    if not isinstance(data, dict):
        logger.warning("codenames file %s is not a JSON object", path)
        return ()
    denylist = data.get("denylist", [])
    if not isinstance(denylist, list):
        logger.warning("codenames file %s 'denylist' is not a list", path)
        return ()
    return tuple(s for s in denylist if isinstance(s, str) and s)


class SecretLeakGate:
    """Refuse vault content that contains known-sensitive substrings.

    Only fires for sources marked ``is_public=true`` in their
    RepoConfig. The full pattern list is the out-of-SC baseline
    (``load_baseline_sensitive_strings``) plus any extras declared in
    ``RepoConfig.sensitive_strings``.

    On hit: returns ``hold`` with the matched substrings in the
    payload. The daemon emits ``obsidian_edit_held`` with the gate
    name so the operator's per-session relay surfaces it.

    On miss: returns ``pass``. The next gate in the chain runs.

    On non-public source: returns ``skip`` (gate is not applicable).

    Match semantics: substring match, case-sensitive. Each pattern is
    checked independently; the held payload names *every* pattern that
    matched, not just the first.
    """

    name = "secret_leak"

    def check(self, ctx: GateContext) -> GateResult:
        is_public = bool(ctx.extras.get("is_public", False))
        if not is_public:
            return GateResult.skipped("source not marked public")

        extras_blocklist: list[str] = list(
            ctx.extras.get("sensitive_strings_extra") or []
        )
        all_patterns = (
            *load_baseline_sensitive_strings(),
            *extras_blocklist,
        )

        matched = self._find_matches(ctx.vault_content, all_patterns)
        if not matched:
            return GateResult.passed()

        logger.warning(
            "secret-leak gate held vault edit at %s; matched substrings: %s",
            ctx.vault_path, sorted(matched),
        )
        return GateResult.held(
            "secret_leak",
            matched_substrings=sorted(matched),
            source_kind=ctx.source_kind,
            source_repo=ctx.source_repo,
            file_relpath=ctx.file_relpath,
            secret_leak=True,
        )

    @staticmethod
    def _find_matches(content: str, patterns: Iterable[str]) -> set[str]:
        """Return the set of patterns from ``patterns`` that appear in
        ``content`` as substrings. Empty patterns are ignored (they
        would match everything and signal a config bug)."""
        return {
            p for p in patterns
            if p and p in content
        }
