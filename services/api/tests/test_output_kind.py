"""Tests for OutputKind enum per ADR-0022.

Verifies that the OutputKind enum includes all expected values and that
the routing layer correctly recognizes documentation as a valid output kind.
"""

from __future__ import annotations

import pytest

from treadmill_api.models import OutputKind


class TestOutputKindEnum:
    """Tests for the OutputKind enum definition."""

    def test_output_kind_has_code(self) -> None:
        """OutputKind includes CODE."""
        assert hasattr(OutputKind, "CODE")
        assert OutputKind.CODE == "code"

    def test_output_kind_has_review(self) -> None:
        """OutputKind includes REVIEW."""
        assert hasattr(OutputKind, "REVIEW")
        assert OutputKind.REVIEW == "review"

    def test_output_kind_has_analysis(self) -> None:
        """OutputKind includes ANALYSIS."""
        assert hasattr(OutputKind, "ANALYSIS")
        assert OutputKind.ANALYSIS == "analysis"

    def test_output_kind_has_plan_doc(self) -> None:
        """OutputKind includes PLAN_DOC."""
        assert hasattr(OutputKind, "PLAN_DOC")
        assert OutputKind.PLAN_DOC == "plan_doc"

    def test_output_kind_has_documentation(self) -> None:
        """OutputKind includes DOCUMENTATION per ADR-0032.

        DOCUMENTATION is distinct from PLAN_DOC; only the latter
        triggers ADR-0021 plan-creation on merge.
        """
        assert hasattr(OutputKind, "DOCUMENTATION")
        assert OutputKind.DOCUMENTATION == "documentation"

    def test_output_kind_values_are_lowercase_snake_case(self) -> None:
        """All OutputKind values follow ADR-0016's canonical-spellings
        discipline: lowercase snake_case."""
        for kind in OutputKind:
            assert kind.value.islower(), f"{kind.value} is not lowercase"
            assert kind.value.replace("_", "").isalnum(), (
                f"{kind.value} contains non-alphanumeric characters"
            )

    def test_output_kind_can_construct_from_string(self) -> None:
        """OutputKind values can be constructed from their string values."""
        assert OutputKind("code") == OutputKind.CODE
        assert OutputKind("review") == OutputKind.REVIEW
        assert OutputKind("analysis") == OutputKind.ANALYSIS
        assert OutputKind("plan_doc") == OutputKind.PLAN_DOC
        assert OutputKind("documentation") == OutputKind.DOCUMENTATION

    def test_documentation_is_distinct_from_plan_doc(self) -> None:
        """DOCUMENTATION and PLAN_DOC are different values.

        Per ADR-0032 Q32.a — documentation is distinct from plan_doc.
        Only the latter triggers ADR-0021 plan-creation on merge.
        """
        assert OutputKind.DOCUMENTATION != OutputKind.PLAN_DOC
        assert OutputKind.DOCUMENTATION.value != OutputKind.PLAN_DOC.value


class TestOutputKindRouting:
    """Tests for OutputKind routing integration."""

    def test_role_response_accepts_documentation_output_kind(self) -> None:
        """The RoleResponse model accepts documentation as an output_kind.

        Verifies that the routing layer recognizes documentation as a
        valid OutputKind value for role responses.
        """
        from treadmill_api.routers.roles import RoleResponse
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc)
        role = RoleResponse(
            id="role-documentarian",
            model="claude-haiku-4-5-20251001",
            system_prompt="Document the codebase",
            output_kind=OutputKind.DOCUMENTATION,
            skills=[],
            hooks=[],
            created_at=now,
            updated_at=now,
        )
        assert role.output_kind == OutputKind.DOCUMENTATION

    def test_role_create_request_accepts_documentation_output_kind(self) -> None:
        """The RoleCreateRequest model accepts documentation as an output_kind.

        Verifies that the API endpoint can receive documentation in
        role creation requests.
        """
        from treadmill_api.routers.roles import RoleCreateRequest

        request = RoleCreateRequest(
            id="role-documentarian",
            model="claude-haiku-4-5-20251001",
            system_prompt="Document the codebase",
            output_kind=OutputKind.DOCUMENTATION,
        )
        assert request.output_kind == OutputKind.DOCUMENTATION

    def test_step_context_accepts_documentation_output_kind(self) -> None:
        """The _RoleBlock in step context accepts documentation.

        Verifies that step context responses correctly serialize
        documentation as an output kind.
        """
        from treadmill_api.routers.steps import _RoleBlock

        role_block = _RoleBlock(
            id="role-documentarian",
            model="claude-haiku-4-5-20251001",
            system_prompt="Document the codebase",
            output_kind=OutputKind.DOCUMENTATION,
            skills=[],
            hooks=[],
        )
        assert role_block.output_kind == OutputKind.DOCUMENTATION

    def test_all_output_kinds_are_valid_in_role_block(self) -> None:
        """All OutputKind values are valid for _RoleBlock.

        Ensures the routing layer's role block can accept all enum values.
        """
        from treadmill_api.routers.steps import _RoleBlock

        for kind in OutputKind:
            role_block = _RoleBlock(
                id=f"role-test-{kind.value}",
                model="claude-haiku-4-5-20251001",
                system_prompt=f"Test role for {kind.value}",
                output_kind=kind,
                skills=[],
                hooks=[],
            )
            assert role_block.output_kind == kind
