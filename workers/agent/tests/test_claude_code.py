"""Claude Code wrapper tests.

We don't invoke the real ``claude`` CLI in unit tests — instead we
override ``CLAUDE_BINARY`` to a small bash stub and assert the wrapper
shells out with the right flags.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from treadmill_agent import claude_code
from treadmill_agent.api_client import PriorStep, Role


def _role(**overrides) -> Role:
    base = dict(
        id="role-author", model="claude-opus-4-7",
        system_prompt="be a coder",
        skills=[], hooks=[],
    )
    base.update(overrides)
    return Role(**base)


def test_compose_prompt_includes_all_sections() -> None:
    role = _role()
    prompt = claude_code._compose_prompt(
        role=role, task_title="Add a thing",
        task_description="implement X", plan_intent="goal of plan",
    )
    assert "Plan intent" in prompt
    assert "goal of plan" in prompt
    assert "Add a thing" in prompt
    assert "implement X" in prompt
    assert "Instructions" in prompt


def test_compose_prompt_omits_intent_when_none() -> None:
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None, plan_intent=None,
    )
    assert "Plan intent" not in prompt


def test_compose_prompt_includes_skill_content() -> None:
    from treadmill_agent.api_client import Skill
    role = _role(skills=[
        Skill(id="s1", name="careful", content="be careful"),
        Skill(id="s2", name="terse", content="be terse"),
    ])
    prompt = claude_code._compose_prompt(
        role=role, task_title="t", task_description=None, plan_intent=None,
    )
    assert "Skills available" in prompt
    assert "be careful" in prompt
    assert "be terse" in prompt
    # Skills appear in declared order.
    assert prompt.index("be careful") < prompt.index("be terse")


# ── prior_steps folding (ADR-0015 multi-step workflows) ─────────────────────


def _prior_step(
    *,
    step_index: int = 0,
    step_name: str = "analyzer",
    role_id: str = "role-feedback-analyzer",
    status: str = "completed",
    output: dict | None = None,
) -> PriorStep:
    """Helper — build a ``PriorStep`` with a default analyzer-shaped
    output. Tests override ``output`` to exercise the various payload
    shapes the prompt-composer must handle."""
    return PriorStep(
        step_index=step_index, step_name=step_name,
        role_id=role_id, status=status,
        output=output,
    )


def test_compose_prompt_includes_prior_step_task_directive_when_present() -> None:
    """ADR-0015 §"Inter-step state passing": the action role consumes
    the analyzer's ``task_directive`` via ``prior_steps[-1].output
    .payload.task_directive``. The prompt-composer folds the directive
    in as a structured block under "Prior step output"."""
    prior = _prior_step(output={
        "summary": "Fix typo in foo.py",
        "decision": "plan-ready",
        "payload": {
            "task_directive": {
                "summary": "Correct the spelling of 'recieve' to 'receive'",
                "files": ["foo.py"],
                "intent": "PR review flagged a spelling mistake on line 12",
            },
        },
    })
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[prior],
    )
    assert "Prior step output" in prompt
    # Summary + intent (from the directive) must surface in the prompt
    # so the action role sees what the analyzer concluded.
    assert "Correct the spelling of 'recieve' to 'receive'" in prompt
    assert "PR review flagged a spelling mistake on line 12" in prompt
    # The structured directive is serialized as JSON so the action
    # role can parse files / scope literally.
    assert "foo.py" in prompt


def test_compose_prompt_omits_prior_steps_section_when_empty() -> None:
    """Single-step workflows (e.g. ``wf-author``) pass ``prior_steps=[]``;
    the "Prior step output" section must not appear."""
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[],
    )
    assert "Prior step output" not in prompt


def test_compose_prompt_uses_most_recent_prior_step_for_directive() -> None:
    """ADR-0015 §"Q15.c": when multiple prior steps exist, the
    immediately-prior step's directive is the one the action role
    consumes. ``prior_steps`` is ordered ascending by ``step_index``,
    so the most recent is ``[-1]`` — not ``[0]``."""
    older = _prior_step(
        step_index=0, step_name="research",
        role_id="role-planner",
        output={
            "summary": "first prior",
            "decision": "plan-ready",
            "payload": {
                "task_directive": {
                    "summary": "OLD-DIRECTIVE-SUMMARY",
                    "intent": "stale intent text",
                },
            },
        },
    )
    newer = _prior_step(
        step_index=1, step_name="analyzer",
        role_id="role-feedback-analyzer",
        output={
            "summary": "second prior",
            "decision": "plan-ready",
            "payload": {
                "task_directive": {
                    "summary": "NEW-DIRECTIVE-SUMMARY",
                    "intent": "fresh intent text",
                },
            },
        },
    )
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[older, newer],
    )
    # The newer directive's summary + intent must appear.
    assert "NEW-DIRECTIVE-SUMMARY" in prompt
    assert "fresh intent text" in prompt
    # The older directive's summary + intent must NOT appear — it has
    # been superseded by the immediately-prior step.
    assert "OLD-DIRECTIVE-SUMMARY" not in prompt
    assert "stale intent text" not in prompt


def test_compose_prompt_handles_prior_step_without_task_directive() -> None:
    """Graceful fallback: when the prior step's ``payload`` carries no
    ``task_directive`` (an action-step output, or an analyzer that
    decided ``no-action-needed`` / ``blocked``), the prompt-composer
    still surfaces ``summary`` + ``decision`` so the current role has
    *some* context — rather than dropping the section entirely and
    losing the prior decision."""
    prior = _prior_step(
        step_name="action",
        role_id="role-code-author",
        output={
            "summary": "pushed branch task/abc",
            "decision": "pushed",
            "payload": {"pr_number": 42},  # action output, no task_directive
        },
    )
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[prior],
    )
    # The section appears.
    assert "Prior step output" in prompt
    # Summary + decision are surfaced as the fallback context.
    assert "pushed branch task/abc" in prompt
    assert "pushed" in prompt
    # No JSON directive block since none was provided.
    assert "Task directive" not in prompt


def test_run_claude_code_passes_model_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoke a stub binary that records its argv to a file. Assert the
    wrapper shells out with the role's model + system_prompt + prompt."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    log = tmp_path / "args.log"

    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        'echo "did the thing"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    result = claude_code.run_claude_code(
        repo_dir=repo_dir, role=_role(),
        task_title="Add a thing", task_description=None, plan_intent=None,
    )
    assert result.summary == "did the thing"
    args = log.read_text().splitlines()
    assert "--print" in args
    assert "--model" in args
    assert "claude-opus-4-7" in args
    assert "--append-system-prompt" in args
    assert "be a coder" in args
    # ``--permission-mode acceptEdits`` is mandatory in headless mode —
    # without it Claude's Edit / Write tools silently no-op (it emits
    # text describing the change without performing it). Discovered
    # during B.11's real-Claude smoke wiring; the worker would otherwise
    # see "no changes staged" on every real-Claude run.
    assert "--permission-mode" in args
    assert "acceptEdits" in args
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    # ``--print`` must come before ``--model`` in argv so the CLI's
    # headless mode is established before the model flag is parsed.
    # Catches a future arg-order regression where flags get reordered
    # in `_compose_prompt` or `run_claude_code`.
    assert args.index("--print") < args.index("--model")


def test_run_claude_code_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 7\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with pytest.raises(claude_code.CodeAuthorError, match="exited 7"):
        claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
        )


def test_find_binary_fallbacks_to_path_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_BINARY", raising=False)
    # PATH lookup should succeed for common binaries; we use 'sh' as a
    # stand-in to confirm shutil.which is consulted.
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: "/usr/bin/sh")
    assert claude_code._find_binary() == "/usr/bin/sh"


def test_find_binary_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_BINARY", raising=False)
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: None)
    with pytest.raises(claude_code.CodeAuthorError, match="not found in PATH"):
        claude_code._find_binary()
