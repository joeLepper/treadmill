"""Tests for :mod:`treadmill_cli.plan_validate`.

The cases mirror the three real plan-authoring bugs shipped in PRs
#58 / #60 / (the abandoned Step 4 first attempt) on 2026-05-28, plus
the rest of the SKILL.md ~line 132 rule set the validator encodes:

- ``alembic upgrade head`` (sandbox-unsafe)
- ``test -f .../20260528_1600_<name>.py`` (format-brittle filename)
- ``cd /home/joe/treadmill/workers/agent`` (dev-machine absolute path)
- plus: cdk/aws/docker/psql/pip-install/npm-install/curl-external

Each rule has a positive (offending) and a negative (clean) case so a
regression in the regex flips at least one assertion.
"""

from __future__ import annotations

import textwrap

import pytest

from treadmill_cli.plan_validate import Violation, validate_plan_doc


# ── Helpers ──────────────────────────────────────────────────────────────────


_BASE_TEMPLATE = textwrap.dedent("""\
    # Plan: Test

    - **Status:** active
    - **Date:** 2026-05-28

    ## Goal
    Test.

    ## Success criteria
    Validator behaves.

    ## Constraints / scope

    ### In scope
    The test gate.

    ### Out of scope
    Everything else.

    ## Sequence of work

    ```yaml
    sequence_of_work:
      - id: only-task
        title: Test task
        workflow: wf-author
        intent: |
          Test.
        scope:
          files: [{scope_files}]
        validation:
          - kind: deterministic
            description: Test gate.
            script: |
              {script}
    ```
""")


def _make_plan(*, script: str, scope_files: str = '"some/file.py"') -> str:
    # ``script`` is interpolated under ``script: |`` so each line must
    # carry the YAML block-scalar's continuation indent. Two trailing
    # spaces also matter for some patterns; keep them explicit in the
    # caller-supplied string.
    indented_script = "\n              ".join(script.splitlines())
    return _BASE_TEMPLATE.format(scope_files=scope_files, script=indented_script)


def _rules(violations: list[Violation]) -> set[str]:
    return {v.rule for v in violations}


# ── Clean plan ───────────────────────────────────────────────────────────────


def test_clean_plan_returns_no_violations() -> None:
    doc = _make_plan(script="uv run pytest tests/test_thing.py -q")
    assert validate_plan_doc(doc) == []


def test_grep_for_function_name_only_is_clean() -> None:
    # Coarse-presence grep is the SKILL.md-recommended pattern.
    doc = _make_plan(script='grep -lE "def materialize\\b" workers/agent/treadmill_agent/repo_deps.py')
    assert validate_plan_doc(doc) == []


def test_grep_for_forbidden_string_in_dockerfile_is_clean() -> None:
    # Real false positive surfaced against 2026-05-27-add-cdk-to-agent-image.md:
    # the script greps for "npm install" inside a Dockerfile as a meta-check,
    # which must not flag as a real npm-install invocation.
    doc = _make_plan(
        script='grep -E "npm install.*aws-cdk|aws-cdk" workers/agent/Dockerfile'
    )
    assert validate_plan_doc(doc) == []


def test_grep_for_absolute_path_string_is_clean() -> None:
    # Same family: greping FOR the string "/home/" is a meta-check, not
    # an actual reference to a dev-machine absolute path.
    doc = _make_plan(script='grep -L "/home/" workers/agent/treadmill_agent/*.py')
    assert validate_plan_doc(doc) == []


# ── Sandbox-unsafe tools ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "script",
    [
        "cd services/api && uv run alembic upgrade head",
        "alembic downgrade -1",
        "alembic stamp head",
    ],
)
def test_alembic_db_commands_flagged_as_sandbox_unsafe(script: str) -> None:
    """Real bug from PR #58 — the 2026-05-28 ADR-0059 Step 1 retry cycle."""
    violations = validate_plan_doc(_make_plan(script=script))
    assert "sandbox-unsafe-tool" in _rules(violations)


@pytest.mark.parametrize(
    "script",
    [
        "cdk synth",
        "cdk deploy MyStack",
        "cdk diff",
    ],
)
def test_cdk_subcommands_flagged(script: str) -> None:
    violations = validate_plan_doc(_make_plan(script=script))
    assert "sandbox-unsafe-tool" in _rules(violations)


def test_aws_cli_flagged() -> None:
    violations = validate_plan_doc(
        _make_plan(script="aws s3 ls s3://some-bucket")
    )
    assert "sandbox-unsafe-tool" in _rules(violations)


@pytest.mark.parametrize(
    "script",
    [
        "docker run --rm alpine echo hi",
        "docker compose up -d",
        "docker exec api ls",
    ],
)
def test_docker_subcommands_flagged(script: str) -> None:
    violations = validate_plan_doc(_make_plan(script=script))
    assert "sandbox-unsafe-tool" in _rules(violations)


def test_psql_flagged() -> None:
    violations = validate_plan_doc(
        _make_plan(script="psql -c 'select 1' postgres://localhost/db")
    )
    assert "sandbox-unsafe-tool" in _rules(violations)


def test_pip_install_flagged() -> None:
    violations = validate_plan_doc(
        _make_plan(script="pip install requests")
    )
    assert "sandbox-unsafe-tool" in _rules(violations)


def test_pip_install_editable_local_is_clean() -> None:
    # ``pip install -e .`` is a local-only install we permit; the
    # SKILL.md rule is about *registry* egress, not local editable.
    doc = _make_plan(script="pip install -e .")
    assert validate_plan_doc(doc) == []


def test_npm_install_pkg_flagged() -> None:
    violations = validate_plan_doc(
        _make_plan(script="npm install left-pad")
    )
    assert "sandbox-unsafe-tool" in _rules(violations)


def test_curl_external_url_flagged() -> None:
    violations = validate_plan_doc(
        _make_plan(script="curl https://example.com/file")
    )
    assert "sandbox-unsafe-tool" in _rules(violations)


def test_curl_localhost_is_clean() -> None:
    # Localhost curl is fine (talking to a worker-local service).
    doc = _make_plan(script="curl http://localhost:8000/healthz")
    assert validate_plan_doc(doc) == []


# ── Absolute paths ───────────────────────────────────────────────────────────


def test_absolute_path_in_script_flagged() -> None:
    """Real bug from PR #66 — Step 4 first attempt."""
    violations = validate_plan_doc(
        _make_plan(script="cd /home/joe/treadmill/workers/agent && pytest -q")
    )
    assert "absolute-path" in _rules(violations)


def test_absolute_path_in_scope_files_flagged() -> None:
    violations = validate_plan_doc(
        _make_plan(
            script="echo ok",
            scope_files='"/home/joe/treadmill/cli/foo.py"',
        )
    )
    assert "absolute-path" in _rules(violations)
    assert any(v.validation_index is None for v in violations)


def test_relative_path_in_scope_is_clean() -> None:
    doc = _make_plan(
        script="echo ok",
        scope_files='"cli/treadmill_cli/foo.py"',
    )
    assert validate_plan_doc(doc) == []


# ── Format-brittleness ───────────────────────────────────────────────────────


def test_test_f_with_timestamped_filename_flagged() -> None:
    """Real bug from PR #60 — Step 1 second-attempt retry."""
    violations = validate_plan_doc(
        _make_plan(
            script="test -f services/api/alembic/versions/20260528_1600_repo_configs_worker_deps.py"
        )
    )
    assert "format-brittle-filename" in _rules(violations)


def test_test_f_without_timestamp_is_clean() -> None:
    # A real, stable filename is fine.
    doc = _make_plan(script="test -f workers/agent/treadmill_agent/repo_deps.py")
    assert validate_plan_doc(doc) == []


def test_grep_for_multi_arg_call_signature_flagged() -> None:
    violations = validate_plan_doc(
        _make_plan(
            script='grep -q "materialize(repo=foo, deps=bar)" workers/agent/treadmill_agent/runner.py'
        )
    )
    assert "format-brittle-grep" in _rules(violations)


# ── llm-judge entries are not script-scanned ────────────────────────────────


def test_llm_judge_validation_is_skipped_safely() -> None:
    doc = textwrap.dedent("""\
        # Plan: Test

        - **Status:** active
        - **Date:** 2026-05-28

        ## Goal
        T.

        ## Success criteria
        T.

        ## Constraints / scope

        ### In scope
        T.

        ### Out of scope
        T.

        ## Sequence of work

        ```yaml
        sequence_of_work:
          - id: only-task
            title: T
            workflow: wf-author
            intent: |
              T.
            scope:
              files: ["x.py"]
            validation:
              - kind: llm-judge
                description: A judgement.
                prompt: |
                  Judge whether the PR meets the criteria — note that
                  `cdk synth` does not apply here because this is a
                  prompt, not a script.
        ```
    """)
    # The cdk-synth string in the prompt MUST NOT be flagged — the
    # validator only scans deterministic scripts.
    assert validate_plan_doc(doc) == []


# ── Per-violation surface ────────────────────────────────────────────────────


def test_violation_carries_task_and_citation() -> None:
    violations = validate_plan_doc(
        _make_plan(script="alembic upgrade head")
    )
    assert violations
    v = violations[0]
    assert v.task_id == "only-task"
    assert v.validation_index == 0
    assert "SKILL.md" in v.citation
