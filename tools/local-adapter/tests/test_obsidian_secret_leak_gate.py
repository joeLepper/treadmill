"""Tests for the secret-leak gate (ADR-0078 §4 secret-leak prevention).

The baseline denylist is loaded OUT OF SOURCE CONTROL (from the
operator-local ``~/.treadmill/codenames.json``, override via
``TREADMILL_CODENAMES_FILE``) — so this test embeds NO real client
names or account-ids. It points the loader at a SYNTHETIC denylist in
``tmp_path`` and exercises the gate + loader against fake patterns
(``acme-client``, ``AcmeCorpHQ``, ``111122223333``).

Coverage:
  * Loader: missing file → empty; unparseable → empty; valid → denylist
  * Public-repo + clean content → pass
  * Public-repo + baseline pattern → held; payload names matches + flag
  * Public-repo + per-repo extra → held
  * Public-repo + baseline AND extra → payload includes ALL matches
  * Non-public repo → skipped
  * Multiple baseline matches → payload includes all
  * Case-sensitive substring match
  * Empty pattern in extras is ignored
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from treadmill_local.obsidian_gates import (
    SecretLeakGate,
    load_baseline_sensitive_strings,
)
from treadmill_local.obsidian_sync import GateContext

# Synthetic denylist — fake stand-ins for the real (out-of-SC) baseline.
_SYNTH_DENYLIST = ["acme-client", "AcmeCorpHQ", "111122223333"]


@pytest.fixture(autouse=True)
def synthetic_codenames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the gate's loader at a synthetic out-of-SC denylist file."""
    p = tmp_path / "codenames.json"
    p.write_text(json.dumps({"denylist": _SYNTH_DENYLIST}), encoding="utf-8")
    monkeypatch.setenv("TREADMILL_CODENAMES_FILE", str(p))
    return p


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


# ── loader: out-of-SC file resolution ────────────────────────────────────


def test_loader_reads_denylist_from_file(synthetic_codenames: Path) -> None:
    assert set(load_baseline_sensitive_strings()) == set(_SYNTH_DENYLIST)


def test_loader_missing_file_is_empty_not_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "TREADMILL_CODENAMES_FILE", str(tmp_path / "does-not-exist.json")
    )
    assert load_baseline_sensitive_strings() == ()


def test_loader_unparseable_file_is_empty_not_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "garbage.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("TREADMILL_CODENAMES_FILE", str(bad))
    assert load_baseline_sensitive_strings() == ()


def test_loader_skips_empty_and_nonstring_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "mixed.json"
    f.write_text(
        json.dumps({"denylist": ["real-token", "", 123, None]}), encoding="utf-8"
    )
    monkeypatch.setenv("TREADMILL_CODENAMES_FILE", str(f))
    assert load_baseline_sensitive_strings() == ("real-token",)


# ── public + clean ────────────────────────────────────────────────────────


def test_public_repo_clean_content_passes() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx("All work and no play makes Jack a dull boy.\n"))
    assert result.decision == "pass"


# ── public + baseline pattern matches ────────────────────────────────────


def test_public_repo_baseline_match_holds_with_substring_in_payload() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx("Note about acme-client integration.\n"))
    assert result.decision == "hold"
    assert result.reason == "secret_leak"
    assert "acme-client" in result.payload["matched_substrings"]
    assert result.payload["secret_leak"] is True
    assert result.payload["source_kind"] == "conform"
    assert result.payload["source_repo"] == "joeLepper/treadmill"


def test_public_repo_baseline_account_id_match_holds() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx("aws account 111122223333 has the bucket\n"))
    assert result.decision == "hold"
    assert "111122223333" in result.payload["matched_substrings"]


def test_public_repo_multiple_baseline_matches_all_in_payload() -> None:
    gate = SecretLeakGate()
    text = "AcmeCorpHQ/acme-client and the 111122223333 account-id\n"
    result = gate.check(_ctx(text))
    assert result.decision == "hold"
    matched = set(result.payload["matched_substrings"])
    assert {"AcmeCorpHQ", "acme-client", "111122223333"} <= matched


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
        "We discussed acme-client and private-codename-x together\n",
        sensitive_strings_extra=["private-codename-x"],
    ))
    assert result.decision == "hold"
    matched = set(result.payload["matched_substrings"])
    assert "acme-client" in matched
    assert "private-codename-x" in matched


# ── non-public repo: gate skipped ────────────────────────────────────────


def test_non_public_repo_skipped_even_with_sensitive_content() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "acme-client reference and 111122223333 account-id here\n",
        is_public=False,
    ))
    assert result.decision == "skip"


# ── case-sensitivity ────────────────────────────────────────────────────


def test_baseline_includes_both_case_variants() -> None:
    """The synthetic baseline lists both ``acme-client`` (lowercase)
    and ``AcmeCorpHQ`` (camelCase). Both match independently; the gate
    is case-sensitive substring match."""
    gate = SecretLeakGate()
    result_lower = gate.check(_ctx("note: acme-client package\n"))
    result_camel = gate.check(_ctx("the AcmeCorpHQ org account\n"))
    assert result_lower.decision == "hold"
    assert "acme-client" in result_lower.payload["matched_substrings"]
    assert result_camel.decision == "hold"
    assert "AcmeCorpHQ" in result_camel.payload["matched_substrings"]


def test_uppercase_variant_not_in_baseline_does_not_match() -> None:
    """``ACMECORPHQ`` is not in the baseline — case-sensitive match.
    Operators who want case-insensitivity add explicit entries via the
    out-of-SC denylist or ``RepoConfig.sensitive_strings``."""
    gate = SecretLeakGate()
    result = gate.check(_ctx("THE ACMECORPHQ TEAM\n"))
    assert result.decision == "pass"


# ── empty pattern in extras is ignored ───────────────────────────────────


def test_empty_extra_pattern_is_ignored() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "completely benign content\n",
        sensitive_strings_extra=["", "private-real-pattern"],
    ))
    assert result.decision == "pass"


def test_empty_extra_does_not_mask_real_match() -> None:
    gate = SecretLeakGate()
    result = gate.check(_ctx(
        "we have private-real-pattern here\n",
        sensitive_strings_extra=["", "private-real-pattern"],
    ))
    assert result.decision == "hold"
    assert "private-real-pattern" in result.payload["matched_substrings"]
    assert "" not in result.payload["matched_substrings"]
