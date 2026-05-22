"""Evaluation harness — score a judge prompt against labeled examples.

Per ADR-0053 (agentic judge-prompt optimization), the optimizer needs a
metric: run a candidate judge prompt over a labeled corpus and compare
its verdicts against the human gold labels. This module provides
``evaluate_judge_prompt`` — the reusable scoring metric the
``role-prompt-optimizer`` calls each iteration, and a regression
metric usable against any judge + labeled corpus.

Mirrors ``validation_runtime.run_llm_judge``'s prompt composition and
JSON-envelope parsing so labeled-example scoring matches production
judge invocation as closely as possible. The parser here is more
permissive than ``_parse_validation_envelope``: it does not enforce
``ValidationVerdict``'s closed ``Literal["pass", "fail"]``, so the
harness also scores judges that emit other vocabularies (e.g. the
architect's ``accept-as-is``/``amend``). Correctness is a
case-insensitive string match against the example's ``gold_verdict``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("treadmill.agent.judge_eval")

_JSON_FENCE_RE = re.compile(
    r"```json5?\s*\n(.*?)\n```",
    flags=re.DOTALL | re.IGNORECASE,
)

# Well-known example keys → the section headers ``run_llm_judge`` uses,
# so the candidate prompt sees inputs in the exact shape it was authored
# against. Unknown keys fall through to a generic ``## <key>`` header.
_KEY_TO_SECTION = {
    "diff": "PR diff",
    "task_spec": "Task spec",
}


@dataclass
class EvalResult:
    score: float
    n: int
    correct: int
    per_example: list[dict]


def _parse_verdict(output: str) -> str | None:
    """Extract the ``verdict`` value from the last JSON fence in
    ``output``. Returns ``None`` on parse failure (no fence, invalid
    JSON, non-dict payload, missing/non-string ``verdict``)."""
    matches = _JSON_FENCE_RE.findall(output or "")
    if not matches:
        return None
    block = matches[-1]
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if not isinstance(verdict, str):
        return None
    return verdict


def _compose_example_prompt(prompt: str, example: dict[str, Any]) -> str:
    """Render the candidate ``prompt`` + the example's non-label inputs
    as ``## <Section>`` blocks, mirroring ``run_llm_judge``'s shape.
    ``gold_verdict`` is excluded — it's the label, not an input the
    judge sees."""
    sections: list[str] = []
    for key, value in example.items():
        if key == "gold_verdict":
            continue
        header = _KEY_TO_SECTION.get(key, key)
        sections.append(f"## {header}\n{value}")
    body = "\n\n".join(sections)
    if body:
        return f"{prompt}\n\n{body}"
    return prompt


def evaluate_judge_prompt(
    prompt: str,
    examples: list[dict],
    *,
    model: str,
    timeout_seconds: int = 30,
) -> EvalResult:
    """Score a candidate judge prompt against labeled examples.

    For each example, compose ``prompt`` + the non-``gold_verdict``
    keys as labeled sections, invoke Claude via
    ``treadmill_agent.claude_code.run_claude``, parse the verdict from
    the returned JSON envelope, and compare it (case-insensitive
    string match) against ``example["gold_verdict"]``. Parse failures
    and ``run_claude`` exceptions both mark the example ``error=True``
    and count as incorrect.

    Args:
        prompt: candidate judge prompt to evaluate.
        examples: labeled examples; each a dict of input fields plus a
            ``gold_verdict`` (str).
        model: Claude model id passed through to ``run_claude``.
        timeout_seconds: per-example timeout for ``run_claude``.

    Returns:
        EvalResult with the fraction correct, raw counts, and
        per-example bookkeeping (``{index, predicted, gold, correct,
        error}``).
    """
    from treadmill_agent import claude_code

    n = len(examples)
    correct = 0
    per_example: list[dict] = []

    for i, example in enumerate(examples):
        gold = str(example.get("gold_verdict", ""))
        composed = _compose_example_prompt(prompt, example)
        try:
            raw = claude_code.run_claude(
                prompt=composed,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            logger.warning("run_claude failed on example %d: %s", i, exc)
            per_example.append(
                {
                    "index": i,
                    "predicted": None,
                    "gold": gold,
                    "correct": False,
                    "error": True,
                }
            )
            continue

        predicted = _parse_verdict(raw)
        if predicted is None:
            per_example.append(
                {
                    "index": i,
                    "predicted": None,
                    "gold": gold,
                    "correct": False,
                    "error": True,
                }
            )
            continue

        is_correct = predicted.strip().lower() == gold.strip().lower()
        if is_correct:
            correct += 1
        per_example.append(
            {
                "index": i,
                "predicted": predicted,
                "gold": gold,
                "correct": is_correct,
                "error": False,
            }
        )

    score = (correct / n) if n > 0 else 0.0
    return EvalResult(score=score, n=n, correct=correct, per_example=per_example)
