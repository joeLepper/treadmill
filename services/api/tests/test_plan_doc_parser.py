"""Unit tests for the plan-doc parser."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from treadmill_api.parsers import (
    PlanDocFormatError,
    PlanFrontmatter,
    TaskScope,
    TaskSpec,
    TaskValidationCheck,
    extract_sequence_yaml,
    parse_plan_doc,
    parse_plan_doc_frontmatter,
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
        script: "alembic upgrade head"
      - kind: llm-judge
        description: "Migration creates users table with id + email + created_at"
        prompt: "Does the migration create a users table with id + email + created_at columns?"
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
        script: "pytest -q"
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
        script: "pytest -q"
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
            validation=[{"kind": "deterministic", "description": "x", "script": "echo test"}],
        ),
        TaskSpec(
            id="t1", title="y", workflow="wf-author", intent="y",
            scope=TaskScope(files=["b.py"]),
            validation=[{"kind": "deterministic", "description": "y", "script": "echo test"}],
        ),
    ]
    validate_unique_task_ids(specs)  # does not raise


def test_validate_unique_task_ids_rejects_duplicates():
    specs = [
        TaskSpec(
            id="t0", title="x", workflow="wf-author", intent="x",
            scope=TaskScope(files=["a.py"]),
            validation=[{"kind": "deterministic", "description": "x", "script": "echo test"}],
        ),
        TaskSpec(
            id="t0", title="y", workflow="wf-author", intent="y",
            scope=TaskScope(files=["b.py"]),
            validation=[{"kind": "deterministic", "description": "y", "script": "echo test"}],
        ),
    ]
    with pytest.raises(PlanDocFormatError, match="duplicate task id"):
        validate_unique_task_ids(specs)


# ── TaskValidationCheck ───────────────────────────────────────────────────────


def test_task_validation_check_deterministic_happy_path():
    """Deterministic checks require a script and forbid prompt."""
    check = TaskValidationCheck(
        kind="deterministic",
        description="tests pass",
        script="pytest",
    )
    assert check.kind == "deterministic"
    assert check.script == "pytest"
    assert check.prompt is None
    assert check.severity == "blocking"
    assert check.timeout_seconds == 30


def test_task_validation_check_llm_judge_happy_path():
    """LLM-judge checks require a prompt and forbid script."""
    check = TaskValidationCheck(
        kind="llm-judge",
        description="code is idiomatic",
        prompt="Does this code follow style conventions?",
        llm_model="claude-opus-4-7",
    )
    assert check.kind == "llm-judge"
    assert check.prompt == "Does this code follow style conventions?"
    assert check.script is None
    assert check.llm_model == "claude-opus-4-7"
    assert check.severity == "blocking"


def test_task_validation_check_rejects_deterministic_without_script():
    """Deterministic checks must have a script."""
    with pytest.raises(ValidationError, match="deterministic requires script"):
        TaskValidationCheck(
            kind="deterministic",
            description="tests pass",
        )


def test_task_validation_check_rejects_deterministic_with_prompt():
    """Deterministic checks must not have a prompt."""
    with pytest.raises(ValidationError, match="deterministic forbids prompt"):
        TaskValidationCheck(
            kind="deterministic",
            description="tests pass",
            script="pytest",
            prompt="This should not be here",
        )


def test_task_validation_check_rejects_llm_judge_without_prompt():
    """LLM-judge checks must have a prompt."""
    with pytest.raises(ValidationError, match="llm-judge requires prompt"):
        TaskValidationCheck(
            kind="llm-judge",
            description="code is idiomatic",
        )


def test_task_validation_check_rejects_llm_judge_with_script():
    """LLM-judge checks must not have a script."""
    with pytest.raises(ValidationError, match="llm-judge forbids script"):
        TaskValidationCheck(
            kind="llm-judge",
            description="code is idiomatic",
            prompt="Is this good?",
            script="echo test",
        )


def test_task_validation_check_custom_severity():
    """Severity can be overridden."""
    check = TaskValidationCheck(
        kind="deterministic",
        description="optional check",
        script="lint",
        severity="advisory",
    )
    assert check.severity == "advisory"


def test_parse_plan_doc_with_deterministic_script():
    """Plan doc with deterministic validation including script field."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "Test"
    workflow: wf-author
    intent: do test
    scope:
      files: [test.py]
    validation:
      - kind: deterministic
        description: "tests pass"
        script: "pytest -q"
        script: "pytest tests/"
"""
    specs = parse_plan_doc(_wrap_plan_md(yaml_body))
    assert len(specs) == 1
    assert specs[0].validation[0].kind == "deterministic"
    assert specs[0].validation[0].script == "pytest tests/"
    assert specs[0].validation[0].prompt is None


def test_parse_plan_doc_with_llm_judge_prompt():
    """Plan doc with llm-judge validation including prompt field."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "Test"
    workflow: wf-author
    intent: do test
    scope:
      files: [test.py]
    validation:
      - kind: llm-judge
        description: "code quality check"
        prompt: "Is the code well written?"
        llm_model: "claude-opus-4-7"
"""
    specs = parse_plan_doc(_wrap_plan_md(yaml_body))
    assert len(specs) == 1
    assert specs[0].validation[0].kind == "llm-judge"
    assert specs[0].validation[0].prompt == "Is the code well written?"
    assert specs[0].validation[0].script is None
    assert specs[0].validation[0].llm_model == "claude-opus-4-7"


def test_parse_plan_doc_rejects_deterministic_missing_script():
    """Parsing fails when deterministic check lacks script."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "Test"
    workflow: wf-author
    intent: do test
    scope:
      files: [test.py]
    validation:
      - kind: deterministic
        description: "tests pass"
"""
    with pytest.raises(ValidationError, match="deterministic requires script"):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_deterministic_with_prompt():
    """Parsing fails when deterministic check includes prompt."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "Test"
    workflow: wf-author
    intent: do test
    scope:
      files: [test.py]
    validation:
      - kind: deterministic
        description: "tests pass"
        script: "pytest -q"
        script: "pytest"
        prompt: "Is it good?"
"""
    with pytest.raises(ValidationError, match="deterministic forbids prompt"):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_llm_judge_missing_prompt():
    """Parsing fails when llm-judge check lacks prompt."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "Test"
    workflow: wf-author
    intent: do test
    scope:
      files: [test.py]
    validation:
      - kind: llm-judge
        description: "code quality"
"""
    with pytest.raises(ValidationError, match="llm-judge requires prompt"):
        parse_plan_doc(_wrap_plan_md(yaml_body))


def test_parse_plan_doc_rejects_llm_judge_with_script():
    """Parsing fails when llm-judge check includes script."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "Test"
    workflow: wf-author
    intent: do test
    scope:
      files: [test.py]
    validation:
      - kind: llm-judge
        description: "code quality"
        prompt: "Is it good?"
        script: "pytest"
"""
    with pytest.raises(ValidationError, match="llm-judge forbids script"):
        parse_plan_doc(_wrap_plan_md(yaml_body))


# ── Frontmatter tests (ADR-0031 Q31.c) ────────────────────────────────────────


def test_parse_plan_doc_frontmatter_absent_returns_defaults() -> None:
    """No leading ``---`` block → empty PlanFrontmatter (auto_merge=None)."""
    md = "# Plan: Test\n\nProse only.\n"
    fm = parse_plan_doc_frontmatter(md)
    assert fm.auto_merge is None


def test_parse_plan_doc_frontmatter_auto_merge_true() -> None:
    md = "---\nauto_merge: true\n---\n\n# Plan: Test\n"
    fm = parse_plan_doc_frontmatter(md)
    assert fm.auto_merge is True


def test_parse_plan_doc_frontmatter_auto_merge_false() -> None:
    md = "---\nauto_merge: false\n---\n\n# Plan: Test\n"
    fm = parse_plan_doc_frontmatter(md)
    assert fm.auto_merge is False


def test_parse_plan_doc_frontmatter_yaml_yes_no_coerce_to_bool() -> None:
    """PyYAML 1.1 coerces bare ``yes`` / ``no`` to Python booleans BEFORE
    pydantic sees them. StrictBool then accepts them correctly."""
    assert parse_plan_doc_frontmatter("---\nauto_merge: yes\n---\n").auto_merge is True
    assert parse_plan_doc_frontmatter("---\nauto_merge: no\n---\n").auto_merge is False


def test_parse_plan_doc_frontmatter_rejects_quoted_string() -> None:
    """Quoted strings remain strings through the YAML layer; StrictBool
    rejects them so authors who quoted by accident fail loudly."""
    for quoted in ('"true"', "'true'", '"false"', "'no'", '"1"'):
        md = f"---\nauto_merge: {quoted}\n---\n\n# Plan: Test\n"
        with pytest.raises(ValidationError, match="auto_merge"):
            parse_plan_doc_frontmatter(md)


def test_parse_plan_doc_frontmatter_rejects_integer() -> None:
    """Integer ``1`` / ``0`` stay ints through YAML; StrictBool rejects."""
    with pytest.raises(ValidationError, match="auto_merge"):
        parse_plan_doc_frontmatter("---\nauto_merge: 1\n---\n")
    with pytest.raises(ValidationError, match="auto_merge"):
        parse_plan_doc_frontmatter("---\nauto_merge: 0\n---\n")


def test_parse_plan_doc_frontmatter_ignores_unrelated_fields() -> None:
    """Tolerates conventional frontmatter (status, trigger, parent, ...).
    Typos in ``auto_merge`` silently default to enabled — see model docstring."""
    md = (
        "---\n"
        "status: drafting\n"
        "trigger: some justification\n"
        "parent: docs/adrs/0099-foo.md\n"
        "auto_merge: false\n"
        "---\n\n"
        "# Plan: Test\n"
    )
    fm = parse_plan_doc_frontmatter(md)
    assert fm.auto_merge is False


def test_parse_plan_doc_frontmatter_empty_block_returns_defaults() -> None:
    """``---\\n---`` with no body is harmless, not an error."""
    md = "---\n---\n\n# Plan: Test\n"
    fm = parse_plan_doc_frontmatter(md)
    assert fm.auto_merge is None


def test_parse_plan_doc_frontmatter_non_mapping_raises_format_error() -> None:
    md = "---\n- just a list\n---\n\n# Plan: Test\n"
    with pytest.raises(PlanDocFormatError, match="mapping"):
        parse_plan_doc_frontmatter(md)


def test_parse_plan_doc_frontmatter_invalid_yaml_raises_format_error() -> None:
    md = "---\nauto_merge: [unclosed\n---\n\n# Plan: Test\n"
    with pytest.raises(PlanDocFormatError, match="failed to parse"):
        parse_plan_doc_frontmatter(md)


# ── Optional workflow:/validation: (task 56c0b353, post-ADR-0087 Phase 5) ─────


def test_parse_plan_doc_accepts_doc_without_workflow_and_validation():
    """Regression for task 56c0b353: both fields are inert post-PR-F/G,
    so a doc that omits them must parse — submitters were including dead
    fields just to pass parsing (medicoder #1329 workaround)."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "Modern minimal task"
    intent: |
      Post-Phase-5 plan doc with no dead fields.
    scope:
      files: [a.py]
"""
    specs = parse_plan_doc(_wrap_plan_md(yaml_body))
    assert len(specs) == 1
    assert specs[0].workflow is None
    assert specs[0].validation is None


def test_parse_plan_doc_still_accepts_doc_with_legacy_fields():
    """Back-compat: present values are accepted (and shape-validated)
    exactly as before — older docs parse unchanged."""
    specs = parse_plan_doc(_wrap_plan_md(_VALID_YAML))
    assert specs[0].workflow == "wf-author"
    assert specs[0].validation is not None
    assert len(specs[0].validation) == 2


def test_parse_plan_doc_still_shape_validates_present_validation_block():
    """A PRESENT validation block keeps full shape validation — only
    absence became legal."""
    yaml_body = """sequence_of_work:
  - id: t0
    title: "x"
    intent: x
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "missing script — still an authoring error"
"""
    with pytest.raises(ValidationError):
        parse_plan_doc(_wrap_plan_md(yaml_body))
