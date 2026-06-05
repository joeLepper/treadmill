"""Tests for ADR-0070 substep 3 gold proposer roles.

Validates that role-architect-gold-proposer and role-validator-gold-proposer
are correctly seeded and their system prompts pin the closed-enum vocabularies
per ADR-0070.
"""

from treadmill_api.models import OutputKind
from treadmill_api.starters import _ROLES_BY_ID, WORKER_MODEL


def test_architect_gold_proposer_role_exists() -> None:
    """role-architect-gold-proposer is seeded and resolvable."""
    role = _ROLES_BY_ID.get("role-architect-gold-proposer")
    assert role is not None, "role-architect-gold-proposer not found in _ROLES_BY_ID"
    assert role["id"] == "role-architect-gold-proposer"
    assert role["model"] == "claude-sonnet-4-6"
    assert role["output_kind"] == OutputKind.ANALYSIS


def test_architect_gold_proposer_prompt_references_adr() -> None:
    """Architect proposer prompt references ADR-0070."""
    role = _ROLES_BY_ID["role-architect-gold-proposer"]
    prompt = role["system_prompt"]
    assert "ADR-0070" in prompt, "Prompt must reference ADR-0070"


def test_architect_gold_proposer_prompt_pins_vocabulary() -> None:
    """Architect proposer prompt pins the closed-enum vocabulary."""
    role = _ROLES_BY_ID["role-architect-gold-proposer"]
    prompt = role["system_prompt"]
    # All four labels must appear as literals in the prompt.
    labels = ("too-permissive", "too-strict", "correct", "exclude")
    for label in labels:
        assert f'"{label}"' in prompt or f"'{label}'" in prompt or f": {label}" in prompt, \
            f"Prompt must reference label '{label}' literally"


def test_validator_gold_proposer_role_exists() -> None:
    """role-validator-gold-proposer is seeded and resolvable."""
    role = _ROLES_BY_ID.get("role-validator-gold-proposer")
    assert role is not None, "role-validator-gold-proposer not found in _ROLES_BY_ID"
    assert role["id"] == "role-validator-gold-proposer"
    assert role["model"] == "claude-sonnet-4-6"
    assert role["output_kind"] == OutputKind.ANALYSIS


def test_validator_gold_proposer_prompt_references_adr() -> None:
    """Validator proposer prompt references ADR-0070."""
    role = _ROLES_BY_ID["role-validator-gold-proposer"]
    prompt = role["system_prompt"]
    assert "ADR-0070" in prompt, "Prompt must reference ADR-0070"


def test_validator_gold_proposer_prompt_pins_vocabulary() -> None:
    """Validator proposer prompt pins the closed-enum vocabulary."""
    role = _ROLES_BY_ID["role-validator-gold-proposer"]
    prompt = role["system_prompt"]
    # All three labels must appear as literals in the prompt.
    labels = ("correct-verdict", "wrong-verdict", "unclear")
    for label in labels:
        assert f'"{label}"' in prompt or f"'{label}'" in prompt or f": {label}" in prompt, \
            f"Prompt must reference label '{label}' literally"
