"""Unit tests for the plan-doc parser."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from treadmill_api.parsers import (
    PlanDocFormatError,
    TaskScope,
    TaskSpec,
    extract_sequence_yaml,
    parse_plan_doc,
)
from treadmill_api.parsers.plan_doc import validate_unique_task_ids


# ── Fixture documents ────────────────────────────────────────────────────────


def _wrap_plan_md(yaml_body: str, *, prose_after: str = "") -> str:
    """Build a minimal plan-doc markdown that contains a sequence_of_work block."""
    return (
        "# Plan: Test\n\n"
        "Some prose describing the plan.\n\n"
        "## sequence_of_work\n\n"
        "```yaml\n"
        f"{yaml_body}\n"
        "```\n\n"
        f"{prose_after}\n"
    )


_VALID_YAML = """sequence_of_work:
  - id: t0
    title: "Add users table migration"
    workflow: wf-author
    intent: |
      Add Alembic migration for the users table.
    scope:
      files:
        - services/api/alembic/versions/<auto>.py
        - services/api/treadmill_api/models/user.py
      services_affected:
        - api
      out_of_scope:
        - any other migrations
    validation:
      - kind: deterministic
        description: "alembic upgrade head runs cleanly"
      - kind: llm-judge
        description: "Migration creates users table with id + email + created_at"
"""


# ── extract_sequence_yaml ─────────────────────────────────────────────────────


def test_extract_sequence_yaml_finds_block_after_heading():
    md = _wrap_plan_md(_VALID_YAML)
    body = extract_sequence_yaml(md)
    assert "sequence_of_work:" in body
    assert "id: t0" in body


def test_extract_sequence_yaml_accepts_yml_fence():
    """The fence may use either ``yaml`` or ``yml``."""
    md = "## sequence_of_work\n\n```yml\nsequence_of_work: []\n```\n"
    body = extract_sequence_yaml(md)
    assert body == "sequence_of_work: []"


def test_extract_sequence_yaml_raises_when_heading_missing():
    md = "# Plan: test\n\nNo heading here.\n\n```yaml\nsequence_of_work: []\n```\n"
    with pytest.raises(PlanDocFormatError, match="does not contain"):
        extract_sequence_yaml(md)


def test_extract_sequence_yaml_raises_when_fence_missing():
    md = "## sequence_of_work\n\nProse, no code fence.\n"
    with pytest.raises(PlanDocFormatError, match="no ```yaml"):
        extract_sequence_yaml(md)


def test_extract_sequence_yaml_picks_first_block_after_heading():
    """When multiple yaml blocks exist, only the one after the heading
    is returned. (And other yaml blocks before the heading are ignored.)"""
    md = (
        "# Plan: test\n\n"
        "```yaml\nfoo: bar\n```\n\n"  # decoy block before heading
        "## sequence_of_work\n\n"
        "```yaml\nsequence_of_work: []\n```\n"
    )
    assert extract_sequence_yaml(md) == "sequence_of_work: []"


# ── parse_plan_doc ────────────────────────────────────────────────────────────


def test_parse_plan_doc_returns_typed_specs():
    specs = parse_plan_doc(_wrap_plan_md(_VALID_YAML))
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, TaskSpec)
    assert spec.id == "t0"
    assert spec.workflow == "wf-author"
    assert spec.title == "Add users table migration"
    assert isinstance(spec.scope, TaskScope)
    assert "services/api/alembic/versions/<auto>.py" in spec.scope.files
    assert spec.scope.services_affected == ["api"]
    assert len(spec.validation) == 2
    assert spec.validation[0].kind == "deterministic"
    assert spec.validation[1].kind == "llm-judge"


def test_parse_plan_doc_handles_multiple_tasks():
    yaml_body = """sequence_of_work:
  - id: t0
    title: "First"
    workflow: wf-author
    intent: do first thing
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "tests pass"
  - id: t1
    title: "Second"
    workflow: wf-author
    depends_on:
      - task.t0.pr_merged
    intent: do second thing
    scope:
      files: [b.py]
    validation:
      - kind: deterministic
        description: "tests still pass"
"""
    specs = parse_plan_doc(_wrap_plan_md(yaml_body))
    assert [s.id for s in specs] == ["t0", "t1"]
    assert specs[1].depends_on == ["task.t0.pr_merged"]


def test_parse_plan_doc_rejects_missing_required_field():
    yaml_body = """sequence_of_work:
  - id: t0
    title: "x"
    workflow: wf-author
    # missing intent
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "ok"
"""
    with pytest.raises(ValidationError):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_empty_files_list():
    yaml_body = """sequence_of_work:
  - id: t0
    title: "x"
    workflow: wf-author
    intent: x
    scope:
      files: []
    validation:
      - kind: deterministic
        description: "ok"
"""
    with pytest.raises(ValidationError):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_empty_validation_list():
    yaml_body = """sequence_of_work:
  - id: t0
    title: "x"
    workflow: wf-author
    intent: x
    scope:
      files: [a.py]
    validation: []
"""
    with pytest.raises(ValidationError):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_unknown_validation_kind():
    yaml_body = """sequence_of_work:
  - id: t0
    title: "x"
    workflow: wf-author
    intent: x
    scope:
      files: [a.py]
    validation:
      - kind: cosmic-ray
        description: "x"
"""
    with pytest.raises(ValidationError):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_extra_field_at_task_level():
    """Strict mode: unknown fields raise."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "x"
    workflow: wf-author
    intent: x
    extra_field: nope
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "x"
"""
    with pytest.raises(ValidationError):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_empty_sequence():
    yaml_body = "sequence_of_work: []"
    with pytest.raises(ValidationError):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_top_level_list_instead_of_mapping():
    """The YAML body must be a mapping with a 'sequence_of_work' key, not a
    bare list — even though the list of tasks is what callers care about."""
    yaml_body = "- id: t0\n  title: x"
    with pytest.raises(PlanDocFormatError, match="must be a mapping"):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_unparseable_yaml():
    yaml_body = "sequence_of_work:\n  - id: t0\n    title: : :"
    with pytest.raises(PlanDocFormatError, match="failed to parse"):
        parse_plan_doc(_wrap_plan_md(yaml_body))


# ── validate_unique_task_ids ─────────────────────────────────────────────────


def test_validate_unique_task_ids_passes_for_unique():
    specs = [
        TaskSpec(
            id="t0", title="x", workflow="wf-author", intent="x",
            scope=TaskScope(files=["a.py"]),
            validation=[{"kind": "deterministic", "description": "x"}],
        ),
        TaskSpec(
            id="t1", title="y", workflow="wf-author", intent="y",
            scope=TaskScope(files=["b.py"]),
            validation=[{"kind": "deterministic", "description": "y"}],
        ),
    ]
    validate_unique_task_ids(specs)  # does not raise


def test_validate_unique_task_ids_rejects_duplicates():
    specs = [
        TaskSpec(
            id="t0", title="x", workflow="wf-author", intent="x",
            scope=TaskScope(files=["a.py"]),
            validation=[{"kind": "deterministic", "description": "x"}],
        ),
        TaskSpec(
            id="t0", title="y", workflow="wf-author", intent="y",
            scope=TaskScope(files=["b.py"]),
            validation=[{"kind": "deterministic", "description": "y"}],
        ),
    ]
    with pytest.raises(PlanDocFormatError, match="duplicate task id"):
        validate_unique_task_ids(specs)
