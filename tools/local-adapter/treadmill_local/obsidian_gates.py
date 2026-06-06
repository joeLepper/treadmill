"""Obsidian-sync gate implementations (ADR-0078 §4).

This module ships the secret-leak gate at v1 — the load-bearing one
because the Treadmill repo is PUBLIC. Other gates (filename validity,
ADR-immutability, no-source, creation-disallowed) ship with the
conform-write task in a sibling PR.

The gate framework (Gate protocol + GateContext + GateResult) lives
in ``obsidian_sync.py``. Gates here implement that protocol.
"""

from __future__ import annotations

import logging
from typing import Iterable

from treadmill_local.obsidian_sync import GateContext, GateResult

logger = logging.getLogger("treadmill_local.obsidian_gates")


# ── Secret-leak gate ────────────────────────────────────────────────────


# Hardcoded baseline patterns the secret-leak gate ALWAYS checks for
# in vault-side content destined for a public repo. The list is
# deliberately short, well-known, and operator-curated rather than
# regex-driven so false positives are rare and the gate behavior is
# obvious. RepoConfig.sensitive_strings adds repo-specific extras on
# top of this baseline.
#
# The substrings here came from the cutover-period scrub work
# (project_treadmill_public_ramjac memory + the 2026-05-26 near-leak
# documented in feedback_never_git_add_dash_a_post_cutover). Adding
# more entries here is a config-only change in followups — no schema
# migration needed.
PUBLIC_REPO_BASELINE_SENSITIVE_STRINGS: tuple[str, ...] = (
    "medicoder",
    "MediCoderHQ",
    "medicoderhq",
    # The public-repo deployment account-id (per the
    # project_treadmill_public_ramjac memory). Hardcoded here so a
    # vault-side push that accidentally pastes the account ID is
    # blocked even if the operator hasn't updated the per-repo list.
    "784379639175",
)


class SecretLeakGate:
    """Refuse vault content that contains known-sensitive substrings.

    Only fires for sources marked ``is_public=true`` in their
    RepoConfig. The full pattern list is the baseline above plus any
    extras declared in ``RepoConfig.sensitive_strings``.

    On hit: returns ``hold`` with the matched substrings in the
    payload. The daemon emits ``obsidian_edit_held`` with the gate
    name so the operator's per-session relay surfaces it.

    On miss: returns ``pass``. The next gate in the chain runs.

    On non-public source: returns ``skip`` (gate is not applicable).

    Match semantics: substring match, case-sensitive. Each baseline
    pattern is checked independently; the held payload names *every*
    pattern that matched, not just the first.
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
            *PUBLIC_REPO_BASELINE_SENSITIVE_STRINGS,
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
