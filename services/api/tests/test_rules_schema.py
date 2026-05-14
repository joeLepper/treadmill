"""Schema validation tests for rules in docs/knowledge-base/rules/.

Per ADR-0006, each rule is a YAML document with a defined structure:
- name, description, status (active|deprecated|superseded-by-<slug>)
- crystallized_from (list of sources)
- applies_to (optional; glob patterns)
- checks (list of deterministic | llm-judge checks)
- remediations (list of actions on check failure)
- references (optional; ADR links, learning citations)

These tests verify:
1. Each rule YAML parses cleanly
2. Schema shape matches ADR-0006 spec
3. applies_to globs are valid
4. Deterministic check scripts exist on disk and are executable
5. LLM judge prompts are non-empty
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).parent.parent.parent.parent
RULES_DIR = REPO_ROOT / "docs" / "knowledge-base" / "rules"


def get_rule_files() -> list[Path]:
    """Collect all .yaml rule files in docs/knowledge-base/rules/."""
    if not RULES_DIR.exists():
        return []
    return sorted(p for p in RULES_DIR.glob("*.yaml") if p.is_file() and p.name != ".gitkeep")


def validate_glob_pattern(pattern: str) -> bool:
    """Verify a glob pattern is syntactically valid for fnmatch."""
    try:
        # Try to compile as a pathlib glob pattern
        Path(".").glob(pattern)
        return True
    except Exception:
        return False


class TestRulesSchema:
    """Test suite for rule schema validation (ADR-0006)."""

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_rule_parses_valid_yaml(self, rule_file: Path):
        """Each rule file must parse as valid YAML."""
        content = rule_file.read_text()
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            pytest.fail(f"{rule_file.name}: YAML parse error: {e}")

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_rule_has_required_fields(self, rule_file: Path):
        """Each rule must have required top-level fields per ADR-0006."""
        rule = yaml.safe_load(rule_file.read_text())
        required_fields = {"name", "description", "status", "crystallized_from", "checks"}
        missing = required_fields - set(rule.keys())
        assert not missing, f"{rule_file.name}: missing required fields: {missing}"

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_rule_name_matches_filename(self, rule_file: Path):
        """Rule name must match the filename (minus .yaml)."""
        rule = yaml.safe_load(rule_file.read_text())
        expected = rule_file.stem
        actual = rule.get("name")
        assert actual == expected, (
            f"{rule_file.name}: name '{actual}' does not match filename '{expected}'"
        )

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_rule_status_is_valid(self, rule_file: Path):
        """Rule status must be active, deprecated, or superseded-by-<slug>."""
        rule = yaml.safe_load(rule_file.read_text())
        status = rule.get("status", "")
        valid_statuses = {"active", "deprecated"}
        is_valid = status in valid_statuses or status.startswith("superseded-by-")
        assert is_valid, f"{rule_file.name}: invalid status '{status}'"

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_rule_crystallized_from_is_nonempty(self, rule_file: Path):
        """crystallized_from must be a non-empty list."""
        rule = yaml.safe_load(rule_file.read_text())
        sources = rule.get("crystallized_from", [])
        assert isinstance(sources, list), (
            f"{rule_file.name}: crystallized_from must be a list, got {type(sources)}"
        )
        assert len(sources) > 0, f"{rule_file.name}: crystallized_from must not be empty"

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_rule_applies_to_globs_are_valid(self, rule_file: Path):
        """If applies_to is present, all globs must be valid."""
        rule = yaml.safe_load(rule_file.read_text())
        applies_to = rule.get("applies_to")
        if applies_to is None:
            return  # Omitted means "all projects" per ADR-0006
        assert isinstance(applies_to, list), (
            f"{rule_file.name}: applies_to must be a list, got {type(applies_to)}"
        )
        for pattern in applies_to:
            assert isinstance(pattern, str), (
                f"{rule_file.name}: applies_to pattern must be string, got {type(pattern)}"
            )
            assert validate_glob_pattern(
                pattern
            ), f"{rule_file.name}: invalid glob pattern '{pattern}'"

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_rule_checks_nonempty_with_valid_types(self, rule_file: Path):
        """checks must be a non-empty list; each check must have type and description."""
        rule = yaml.safe_load(rule_file.read_text())
        checks = rule.get("checks", [])
        assert isinstance(checks, list), (
            f"{rule_file.name}: checks must be a list, got {type(checks)}"
        )
        assert len(checks) > 0, f"{rule_file.name}: checks must not be empty"

        for i, check in enumerate(checks):
            assert isinstance(check, dict), (
                f"{rule_file.name}: check[{i}] must be a dict, got {type(check)}"
            )
            assert "id" in check, f"{rule_file.name}: check[{i}] missing 'id'"
            assert "type" in check, f"{rule_file.name}: check[{i}] missing 'type'"
            assert "description" in check, f"{rule_file.name}: check[{i}] missing 'description'"
            assert check["type"] in {
                "deterministic",
                "llm-judge",
            }, f"{rule_file.name}: check[{i}] invalid type '{check['type']}'"

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_deterministic_checks_have_script(self, rule_file: Path):
        """Each deterministic check must reference a script that exists."""
        rule = yaml.safe_load(rule_file.read_text())
        checks = rule.get("checks", [])

        for check in checks:
            if check.get("type") != "deterministic":
                continue

            script_path = check.get("script")
            assert script_path, f"{rule_file.name}: deterministic check '{check['id']}' missing 'script'"
            assert isinstance(
                script_path, str
            ), f"{rule_file.name}: check '{check['id']}' script must be string"

            # Resolve relative to repo root
            full_path = REPO_ROOT / script_path
            assert full_path.exists(), (
                f"{rule_file.name}: check '{check['id']}' script not found at {script_path}"
            )
            assert full_path.is_file(), (
                f"{rule_file.name}: check '{check['id']}' script is not a file: {script_path}"
            )

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_deterministic_checks_script_is_executable(self, rule_file: Path):
        """Each deterministic check script must be executable."""
        rule = yaml.safe_load(rule_file.read_text())
        checks = rule.get("checks", [])

        for check in checks:
            if check.get("type") != "deterministic":
                continue

            script_path = check.get("script")
            full_path = REPO_ROOT / script_path
            # Check if executable bit is set
            assert full_path.stat().st_mode & 0o111, (
                f"{rule_file.name}: check '{check['id']}' script is not executable: {script_path}"
            )

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_llm_judge_checks_have_nonempty_prompt(self, rule_file: Path):
        """Each llm-judge check must have a non-empty prompt."""
        rule = yaml.safe_load(rule_file.read_text())
        checks = rule.get("checks", [])

        for check in checks:
            if check.get("type") != "llm-judge":
                continue

            prompt = check.get("prompt", "").strip()
            assert prompt, (
                f"{rule_file.name}: llm-judge check '{check['id']}' has empty prompt"
            )

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_checks_have_severity(self, rule_file: Path):
        """Each check must declare a severity."""
        rule = yaml.safe_load(rule_file.read_text())
        checks = rule.get("checks", [])

        for check in checks:
            severity = check.get("severity")
            assert severity, f"{rule_file.name}: check '{check['id']}' missing 'severity'"
            assert severity in {
                "blocking",
                "warning",
                "advisory",
            }, f"{rule_file.name}: check '{check['id']}' invalid severity '{severity}'"

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_remediations_reference_valid_checks(self, rule_file: Path):
        """Each remediation's 'on' field must reference a valid check:outcome pair."""
        rule = yaml.safe_load(rule_file.read_text())
        checks = rule.get("checks", [])
        check_ids = {c["id"] for c in checks}

        remediations = rule.get("remediations", [])
        assert isinstance(remediations, list), (
            f"{rule_file.name}: remediations must be a list"
        )

        for remediation in remediations:
            on = remediation.get("on")
            assert on, f"{rule_file.name}: remediation missing 'on' field"
            # Parse 'on' as '<check-id>:fail' or '<check-id>:uncertain'
            match = re.match(r"^([a-z0-9-]+):(fail|uncertain)$", on)
            assert match, (
                f"{rule_file.name}: remediation 'on' field invalid format: '{on}' "
                "(expected '<check-id>:fail' or '<check-id>:uncertain')"
            )
            check_id = match.group(1)
            assert (
                check_id in check_ids
            ), f"{rule_file.name}: remediation references unknown check '{check_id}'"

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_remediations_have_action_and_target(self, rule_file: Path):
        """Each remediation must have 'action' and 'target' fields."""
        rule = yaml.safe_load(rule_file.read_text())
        remediations = rule.get("remediations", [])

        for remediation in remediations:
            assert "action" in remediation, (
                f"{rule_file.name}: remediation '{remediation.get('on')}' missing 'action'"
            )
            assert remediation["action"] in {
                "block-merge",
                "warn",
                "comment-on-pr",
                "open-task",
                "notify-human",
            }, f"{rule_file.name}: invalid action '{remediation['action']}'"

            assert "target" in remediation, (
                f"{rule_file.name}: remediation '{remediation.get('on')}' missing 'target'"
            )

    @pytest.mark.parametrize("rule_file", get_rule_files(), ids=lambda p: p.name)
    def test_references_are_strings_or_lists(self, rule_file: Path):
        """The references field, if present, must be a list of strings."""
        rule = yaml.safe_load(rule_file.read_text())
        references = rule.get("references")
        if references is None:
            return

        assert isinstance(references, list), (
            f"{rule_file.name}: references must be a list"
        )
        for ref in references:
            assert isinstance(ref, str), (
                f"{rule_file.name}: each reference must be a string, got {type(ref)}"
            )


def test_rule_files_exist():
    """At least one rule file must exist (sanity check for test setup)."""
    rules = get_rule_files()
    assert len(rules) > 0, f"No rule files found in {RULES_DIR}"
