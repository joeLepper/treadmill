"""Tests for the secret-leak gate (ADR-0078 §4 secret-leak prevention).

Coverage:

  * Public-repo + clean content → pass
  * Public-repo + content containing a baseline pattern → held; payload
    names the matched substring(s) and carries ``secret_leak=true``
  * Public-repo + content containing a per-repo extra → held
  * Public-repo + content containing both baseline and extra matches →
    payload includes ALL matches (not just first)
  * Non-public repo → skipped (gate not applicable)
  * Multiple baseline matches → payload includes all
  * Case-sensitive: ``MediCoderHQ`` (baseline) matches; ``medicoderhq``
    is ALSO in baseline (lowercase variant explicitly listed); a totally
    different case (``MEDICODERHQ``) does NOT match (not in baseline)
  * Empty pattern in the extras list is ignored (would otherwise match
    everything trivially)

Pure unit tests; no IO, no DB.
"""

from __future__ import annotations

from pathlib import Path

from treadmill_local.obsidian_gates import (
    PUBLIC_REPO_BASELINE_SENSITIVE_STRINGS,
    SecretLeakGate,
)
from treadmill_local.obsidian_sync import GateContext


def _ctx(
    content: str,
    *,
    is_public: bool = True,
    sensitive_strings_extra: list[str] | None = None,
) -> GateContext:
    return GateContext(
        vault_path=Path("/vault/lepper/treadmill/plans/foo.md"),
        source_kind="conform",
        source_repo="joeLepper/treadmill",
        file_relpath="plans/foo.md",
        vault_content=content,
        source_content="",
        source_hash=None,
        sidecar_entry=None,
        extras={
            "is_public": is_public,
            "sensitive_strings_extra": sensitive_strings_extra or [],
        },
    )


# ── public + clean ────────────────────────────────────────────────────────


def test_public_repo_clean_content_passes() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx("All work and no play makes Jack a dull boy.\n"))
    assert result.decision == "pass"


# ── public + baseline pattern matches ────────────────────────────────────


def test_public_repo_baseline_match_holds_with_substring_in_payload() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx("Note about medicoder integration.\n"))
    assert result.decision == "hold"
    assert result.reason == "secret_leak"
    assert "medicoder" in result.payload["matched_substrings"]
    assert result.payload["secret_leak"] is True
    assert result.payload["source_kind"] == "conform"
    assert result.payload["source_repo"] == "joeLepper/treadmill"


def test_public_repo_baseline_account_id_match_holds() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx("aws account 784379639175 has the bucket\n"))
    assert result.decision == "hold"
    assert "784379639175" in result.payload["matched_substrings"]


def test_public_repo_multiple_baseline_matches_all_in_payload() -> None:
    gate = SecretLeakGate()
    text = "MediCoderHQ/medicoder and the 784379639175 account-id\n"
    result = gate.check(_ctx(text))
    assert result.decision == "hold"
    matched = set(result.payload["matched_substrings"])
    assert {"MediCoderHQ", "medicoder", "784379639175"} <= matched


# ── public + per-repo extra match ────────────────────────────────────────


def test_public_repo_extra_pattern_match_holds() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "Reference to private-codename-x in the doc\n",
        sensitive_strings_extra=["private-codename-x"],
    ))
    assert result.decision == "hold"
    assert "private-codename-x" in result.payload["matched_substrings"]


def test_public_repo_baseline_and_extra_match_both_in_payload() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "We discussed medicoder and private-codename-x together\n",
        sensitive_strings_extra=["private-codename-x"],
    ))
    assert result.decision == "hold"
    matched = set(result.payload["matched_substrings"])
    assert "medicoder" in matched
    assert "private-codename-x" in matched


# ── non-public repo: gate skipped ────────────────────────────────────────


def test_non_public_repo_skipped_even_with_sensitive_content() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "medicoder reference and 784379639175 account-id here\n",
        is_public=False,
    ))
    assert result.decision == "skip"


# ── case-sensitivity ────────────────────────────────────────────────────


def test_baseline_includes_both_case_variants_of_medicoder() -> None:
    """The baseline explicitly lists both ``medicoder`` (lowercase
    variant used in package + dir names) and ``MediCoderHQ`` (the
    org/owner-name camelCase variant). Both must match independently."""
    gate = SecretLeakGate()
    result_lower = gate.check(_ctx("note: medicoder package\n"))
    result_camel = gate.check(_ctx("the MediCoderHQ org account\n"))
    assert result_lower.decision == "hold"
    assert "medicoder" in result_lower.payload["matched_substrings"]
    assert result_camel.decision == "hold"
    assert "MediCoderHQ" in result_camel.payload["matched_substrings"]


def test_uppercase_variant_not_in_baseline_does_not_match() -> None:
    """``MEDICODERHQ`` is not in the baseline list — the gate is
    case-sensitive substring match. Operators who care about
    case-insensitivity can add explicit upper-cased entries via
    ``RepoConfig.sensitive_strings``."""
    gate = SecretLeakGate()
    result = gate.check(_ctx("THE MEDICODERHQ TEAM\n"))
    assert result.decision == "pass"


# ── empty pattern in extras is ignored ───────────────────────────────────


def test_empty_extra_pattern_is_ignored() -> None:
    """An empty string in ``sensitive_strings_extra`` would substring-
    match every content; the gate filters it out so a config bug
    doesn't block every vault write."""
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "completely benign content\n",
        sensitive_strings_extra=["", "private-real-pattern"],
    ))
    assert result.decision == "pass"


def test_empty_extra_does_not_mask_real_match() -> None:
    """An empty extra in the list shouldn't ALSO hide a real match
    elsewhere in the list."""
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "we have private-real-pattern here\n",
        sensitive_strings_extra=["", "private-real-pattern"],
    ))
    assert result.decision == "hold"
    assert "private-real-pattern" in result.payload["matched_substrings"]
    # The empty pattern is not reported as a match.
    assert "" not in result.payload["matched_substrings"]


# ── baseline shape ───────────────────────────────────────────────────────


def test_baseline_includes_known_load_bearing_substrings() -> None:
    """The baseline must include the load-bearing substrings the
    Treadmill repo's public-cutover scrub relied on. Adding more
    entries here is fine; removing any of these requires a
    sibling-ADR-or-learning justification because they're the
    historical reason the gate exists."""
    baseline = set(PUBLIC_REPO_BASELINE_SENSITIVE_STRINGS)
    # The medicoder slug variants from the project_treadmill_public_ramjac
    # memory's scrub work.
    assert "medicoder" in baseline
    assert "MediCoderHQ" in baseline
    # The account-id Bert flagged in the 2026-06-05 ADR-0078 review.
    assert "784379639175" in baseline
