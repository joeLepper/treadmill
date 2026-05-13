---
status: drafting
trigger: ADR-0027 drafted 2026-05-12 in response to the PR #10 deathloop. Tourniquet 8ff414c shipped 2026-05-12; this plan implements the durable structured-JSON envelope. Holding at status:drafting until operator resolves Q27.a-d in ADR-0027.
parent: docs/adrs/0027-structured-json-for-review-output.md
---

# Plan: structured JSON envelope for review-kind output (ADR-0027)

Single-disposition refactor: replace the prose `VERDICT:` marker
with a Pydantic-validated fenced JSON block. The tourniquet
(commit `8ff414c`) keeps the loop alive while this lands; once a
smoke cycle proves the JSON path holds, the tourniquet regex is
deleted.

## Goal

After this plan executes:

1. `role-reviewer` emits its terminal verdict as a fenced JSON
   block matching `ReviewVerdict` (`verdict` ∈ closed value-set +
   `rationale` string).
2. `runner_dispositions/review.py` parses the JSON block first,
   falls through to the regex tourniquet only on parse / validation
   failure, then to the safe `comment` default.
3. Parse-failure paths emit a structured warning
   (`review.json_parse_failed`) the operator can grep for.
4. The fenced block is stripped from the body posted via `gh pr review`
   so the PR-page reader sees clean prose.
5. The tourniquet regex is **kept** at landing time; deletion is a
   follow-up after Q27.a's bar is met.

## Constraints / scope

### In scope
- `workers/agent/treadmill_agent/runner_dispositions/review.py` — add
  `ReviewVerdict` Pydantic model + JSON-block parser + fence-strip
  helper.
- `services/api/treadmill_api/starters.py` — rewrite the
  `role-reviewer.system_prompt` to instruct the JSON fence.
  (Pending ADR-0028 resolution: depending on Q28.a's answer, the
  prompt edit may also need a `treadmill role update role-reviewer
  --prompt-from-file <path>` invocation against any deployed
  environments.)
- `workers/agent/tests/test_runner_dispositions.py` — add JSON-path
  happy-path tests + JSON-fallback-to-regex tests +
  invalid-verdict-falls-to-default tests.
- `services/api/tests/test_starters.py` — update the prompt-content
  assertion to expect the JSON instruction (the existing test
  pins the "no markdown bold" wording).

### Out of scope
- Removing the regex tourniquet (separate follow-up after Q27.a).
- Other output kinds' envelope shape (review is the only
  prose-grep disposition per the 2026-05-12 audit).
- `claude --output-format json` exploration (banked as a separate
  ADR per ADR-0027 §"Alternative C").
- DB-authoritative prompt edit workflow — that's ADR-0028's plan.

## Sequence of work

```yaml
sequence_of_work:
  - id: review-pydantic-model
    title: Add the ReviewVerdict Pydantic model + JSON-block parser
    workflow: wf-author
    intent: |
      In ``workers/agent/treadmill_agent/runner_dispositions/review.py``:

      1. Add a ``ReviewVerdict`` Pydantic model with two fields:
         ``verdict: Literal["approve", "request_changes", "comment"]``
         and ``rationale: str``. The closed value-set on ``verdict``
         lets Pydantic reject anything else; ``rationale`` is required
         (operator may set a max length per Q27.b — default
         ``max_length=4000`` if unresolved).

      2. Add a ``_extract_json_block(summary: str) -> str | None``
         helper that returns the contents of the LAST
         ```` ```json ... ``` ```` fenced block in the summary,
         or ``None`` if no such block exists. Tolerate ``json5``,
         ``JSON``, mixed case in the fence language tag; reject
         non-JSON fences (e.g. ```` ```yaml ```` should not match).

      3. Rewrite ``_parse_verdict_marker`` to:
           a. Try ``_extract_json_block`` → ``json.loads`` →
              ``ReviewVerdict.model_validate``. On success, return
              ``(verdict, rationale)`` (note: signature change —
              the caller updates accordingly).
           b. On any ValidationError / JSONDecodeError, log a
              structured warning ``review.json_parse_failed`` (use
              ``logger.warning`` with ``extra={"reason": ...}``) and
              fall through to the regex tourniquet (the existing
              ``_normalize_verdict_line`` + ``_VERDICT_INNER_RE``
              path). The tourniquet stays in place for one full
              release cycle per Q27.a.
           c. On all-paths-fail, return the safe default
              (``"comment"``, no rationale).

      4. Add a ``_strip_json_block(summary: str) -> str`` helper
         that removes the last JSON fence from the summary so it
         isn't visible on the PR page. The handler passes the
         stripped body to ``gh.pr_review``.

      5. Update ``handle(ctx)`` to call the new parser, pass the
         stripped body to ``gh.pr_review``, and include
         ``rationale`` in the envelope ``payload``.

      Tests: extend ``tests/test_runner_dispositions.py``:
        * ``test_parse_verdict_picks_from_json_fence_happy_path``
        * ``test_parse_verdict_falls_through_to_regex_on_invalid_json``
        * ``test_parse_verdict_logs_warning_on_invalid_json``
        * ``test_parse_verdict_rejects_unknown_verdict_in_json``
        * ``test_strip_json_block_leaves_clean_prose``
        * ``test_handler_posts_body_without_json_fence``
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/review.py
        - workers/agent/tests/test_runner_dispositions.py
    depends_on: []
    branch_hint: feat/review-json-envelope
    pr_template: |
      Implements ADR-0027 phase 1. JSON-fence path lands ahead of
      the regex tourniquet (which stays — separate follow-up to
      delete once Q27.a's bar is met).

  - id: review-prompt-rewrite
    title: Rewrite role-reviewer to emit the JSON fence
    workflow: wf-author
    intent: |
      In ``services/api/treadmill_api/starters.py``, rewrite the
      ``role-reviewer.system_prompt`` to instruct the model to end
      its output with a fenced JSON block of exactly this shape:

      ```json
      {"verdict": "approve" | "request_changes" | "comment", "rationale": "..."}
      ```

      Keep the existing "Most reviews should land at ``approve`` or
      ``request_changes``" guidance from the current prompt. Remove
      the ``VERDICT:`` marker instructions entirely (the tourniquet
      regex remains in code as the fallback parser but is no longer
      the model's instructed format).

      Also update ``services/api/tests/test_starters.py`` to assert
      the new prompt mentions ``json`` (case-insensitive) at least
      once and does NOT mention ``VERDICT:`` (the prompt-side marker
      is gone even while the runner-side parser tolerates it).

      Note for the operator: post-ADR-0028, this prompt update will
      be deliverable via ``treadmill role update`` against any
      deployed env rather than a code-edit-then-seed dance. Pre-
      ADR-0028, the operator runs ``treadmill workflows seed-starters
      --reset-prompts-from-code`` (or its equivalent — to be
      determined by ADR-0028 Q28.b) after merging this change.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters.py
    depends_on:
      - task.review-pydantic-model.pr_merged
    branch_hint: feat/role-reviewer-json-prompt
    pr_template: |
      Implements ADR-0027 phase 2. Drops the ``VERDICT:`` prose
      marker from the prompt; the runner parses the JSON fence
      first, regex tourniquet second.

  - id: review-smoke-validation
    title: Smoke-test the JSON envelope end-to-end + measure parse rate
    workflow: wf-validate
    intent: |
      Open + merge a small no-op PR that triggers a wf-review run
      on the freshly-deployed stack. Confirm via the API logs:
        * ``review`` disposition parsed the JSON fence (no
          ``review.json_parse_failed`` warning).
        * The posted PR review body has no visible JSON fence.
        * ``gh pr view <n> --json reviews`` shows the verdict as
          ``approve`` or ``request_changes`` per the model's call.

      Run this smoke at least 10 times (re-merge whitespace edits)
      to collect a parse-success rate. Per Q27.a, the bar for
      tourniquet deletion is N consecutive runs without falling to
      the regex path; this task collects the data, the operator
      makes the call.
    scope:
      files: []
    depends_on:
      - task.review-prompt-rewrite.pr_merged
    pr_template: |
      Smoke validation for ADR-0027. No code change; documents the
      observed parse rate + decides whether the regex tourniquet is
      safe to remove.
```
