---
auto_merge: false
status: active
---

# Plan: ramjac onboard plumbing — endpoint + CLI (ADR-0051 steps 1–2)

- **Status:** active
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0051 (operator-initiated bootstrap / client-side discovery), ADR-0050 (onboarding), ADR-0049 (App auth)
- **Related plans:** 2026-05-21-ramjac-bootstrap-smoke (the end-to-end smoke test)

## Goal

Build the two pieces that let an operator onboard a repo from inside its
checkout (ADR-0051 steps 1–2): a deployment **onboard API endpoint** and the
**`treadmill-local repo onboard`** CLI that does client-side discovery and posts
to it. Reuses the merged `OnboardingStore` / `repo_profile` / `repo_config`.

**Worker-driven, `auto_merge: false`:** a second orchestrator session is live, so
this runs as isolated worker tasks; the operator merges each PR by hand only when
it's clean and conflict-free with the other session's work.

## Shared request contract (both tasks honor this)

```
POST /api/v1/onboarding/repos
{
  "repo": "owner/name",
  "mode": "conform" | "adapt" | null,        # null → server picks via recommend_mode
  "auto_merge_blocked": true,
  "profile": {
    "repo": "owner/name", "languages": [..], "build_command": str|null,
    "test_command": str|null, "lint_command": str|null, "doc_paths": [..],
    "components": [..], "ci": str|null, "has_agent_context": bool
  }
}
→ 200 {"repo", "mode", "auto_merge_blocked"}
```

## Success criteria

- The endpoint persists a posted profile + config via `OnboardingStore`
  (`repo_profiles` + `repo_configs` rows), defaulting `mode` via `recommend_mode`
  when null; returns the resolved mode.
- The CLI, run from inside a repo, infers `owner/name` from the git remote,
  builds a minimal `repo_profile` from the checkout, and POSTs the contract.
- Both ship with passing tests (no live DB / network needed) and updated
  component AGENT.md.

## Constraints / scope

### In scope
The two additive tasks below.

### Out of scope
Running the smoke test itself; server-side `wf-discover`; rich/clever discovery
(minimal best-effort profile is fine for v1); the CDK S3 bucket; conform-mode
doc seeding.

### Budget
Two tasks. Keep discovery minimal — the point is the plumbing, not perfect
profiling.

## sequence_of_work

```yaml
sequence_of_work:
  - id: onboard-api-endpoint
    title: Onboard API endpoint — POST /api/v1/onboarding/repos (ADR-0051)
    workflow: wf-author
    intent: |
      Add the deployment-side onboard endpoint that registers a repo by
      persisting a posted profile + config. Build on the MERGED
      ``treadmill_api/onboarding_store.py`` (``OnboardingStore`` with
      ``async upsert_repo_config(session, RepoConfig)`` /
      ``upsert_repo_profile(session, RepoProfile)``),
      ``treadmill_api/repo_profile.py`` (``from_dict``, ``recommend_mode``),
      and ``treadmill_api/repo_config.py`` (``RepoConfig``). Read those first.

      Create a NEW router ``services/api/treadmill_api/routers/onboarding.py``
      with ``POST /api/v1/onboarding/repos`` (honor the request contract in the
      plan above). Match the existing router patterns: a pydantic request model,
      the DB session via
      ``from treadmill_api.dependencies_db import get_session`` and
      ``session: Annotated[AsyncSession, Depends(get_session)]`` (see
      ``routers/tasks.py``). Handler logic:
        - build ``RepoProfile`` via ``repo_profile.from_dict(body.profile)``
          (ensure ``profile["repo"]`` defaults to ``body.repo``);
        - resolve ``mode`` = ``body.mode or recommend_mode(profile)`` and
          validate it is "conform"|"adapt";
        - build ``RepoConfig(repo=body.repo, mode=mode,
          auto_merge_blocked=body.auto_merge_blocked,
          test_command=profile.test_command,
          lint_command=profile.lint_command)``;
        - ``await store.upsert_repo_profile(session, profile)`` then
          ``upsert_repo_config(session, config)``; ``await session.commit()``;
        - return ``{repo, mode, auto_merge_blocked}``.
      Register the router in ``services/api/treadmill_api/app.py`` (mirror an
      existing ``include_router`` line; import at top with the others).

      TESTS — NEW ``services/api/tests/test_onboarding_router.py`` using FastAPI
      ``TestClient`` with ``app.dependency_overrides[get_session]`` returning a
      stub/mock session (NO live DB): assert POST with a full profile returns
      200 with the expected mode (test both an explicit mode and mode=null →
      recommend_mode), and that the store upserts were invoked (patch
      ``OnboardingStore`` or inject a fake). Match how other router tests
      construct the app + override deps.

      DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``services/api/AGENT.md`` — add the onboarding router to Key surfaces +
      a Recent-changes entry.

      Additive: do NOT modify ``onboarding_store.py`` or ``models/onboarding.py``.
      If editing ``app.py`` breaks an existing app-factory/route test, that test
      file is in-scope to update (it isn't listed below only because it may not
      exist; add it to your change if needed).
    scope:
      files:
        - services/api/treadmill_api/routers/onboarding.py
        - services/api/treadmill_api/app.py
        - services/api/tests/test_onboarding_router.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/onboarding_store.py
        - services/api/treadmill_api/models/onboarding.py
    validation:
      - kind: deterministic
        description: |
          App imports with the router registered; the router test passes.
        script: |
          cd services/api \
            && uv run python -c "import treadmill_api.app" \
            && grep -q "onboarding" treadmill_api/app.py \
            && uv run pytest tests/test_onboarding_router.py -q

  - id: repo-onboard-cli
    title: treadmill-local repo onboard — client-side discovery + post (ADR-0051)
    workflow: wf-author
    intent: |
      Add the operator entrypoint that onboards the repo in the current working
      directory. ADDITIVE to the ``treadmill-local`` CLI. Do NOT import
      ``treadmill_api`` (keep the CLI decoupled — build a plain dict and POST;
      the endpoint owns the schema + recommend_mode).

      Create a NEW module
      ``tools/local-adapter/treadmill_local/onboard.py`` with pure, testable
      helpers:
        - ``infer_repo(remote_url: str) -> str`` — parse ``owner/name`` from a
          git remote URL (handle ``git@github.com:owner/name.git`` and
          ``https://github.com/owner/name(.git)`` forms).
        - ``build_profile(root: Path) -> dict`` — a MINIMAL best-effort
          ``repo_profile`` dict matching the contract: languages (from file
          extensions, top few), build/test/lint commands (best-effort from
          common markers — pyproject/uv→"uv run pytest", package.json→npm,
          Makefile; ``None`` when unknown), doc_paths (README*, docs/*,
          AGENT.md if present, capped), components (top-level dirs, capped),
          ci ("github-actions" if ``.github/workflows`` exists else None),
          has_agent_context (any AGENT.md present). Keep it simple — None/empty
          is acceptable.
        - ``onboard_payload(repo, profile, *, mode, auto_merge_blocked) -> dict``
          — assemble the request body per the contract.

      Then add a ``repo onboard`` command to
      ``tools/local-adapter/treadmill_local/cli.py`` (under the existing
      ``repo_app`` group): resolve the cwd's origin remote
      (``git remote get-url origin``), ``infer_repo`` + ``build_profile`` +
      ``onboard_payload`` (defaults: ``mode=None`` so the server recommends,
      ``auto_merge_blocked=True`` — never auto-merge an external repo by
      default), and POST to ``{api_url}/api/v1/onboarding/repos`` where
      ``api_url`` is a ``--api-url`` option defaulting to
      ``http://localhost:8088``. Print the resolved repo, mode, and the response.

      TESTS — NEW ``tools/local-adapter/tests/test_onboard.py``: unit-test
      ``infer_repo`` for ssh + https (+ .git suffix) forms; ``build_profile``
      against a ``tmp_path`` fixture with a couple of files (assert
      has_agent_context flips when an AGENT.md exists, ci detects
      ``.github/workflows``); ``onboard_payload`` shape. NO network, NO real git.

      DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``tools/local-adapter/AGENT.md`` — add ``onboard.py`` + the
      ``repo onboard`` command to Key surfaces + a Recent-changes entry.

      If adding the command breaks an existing cli test (e.g. one asserting the
      command set), that test file is in-scope to update.
    scope:
      files:
        - tools/local-adapter/treadmill_local/onboard.py
        - tools/local-adapter/treadmill_local/cli.py
        - tools/local-adapter/tests/test_onboard.py
        - tools/local-adapter/AGENT.md
      out_of_scope:
        - tools/local-adapter/treadmill_local/deployment_config.py
        - tools/local-adapter/treadmill_local/runtime.py
    validation:
      - kind: deterministic
        description: |
          The onboard module + CLI command exist and the unit tests pass.
        script: |
          cd tools/local-adapter \
            && grep -q "def infer_repo" treadmill_local/onboard.py \
            && grep -q "onboard" treadmill_local/cli.py \
            && uv run pytest tests/test_onboard.py -q
```

## Risks / unknowns

- **Shared-file edits** (`app.py`, `cli.py`) could conflict with the other
  orchestrator's PRs — check before merging each.
- **Discovery quality** is intentionally minimal for v1; richer profiling is the
  `wf-discover` productionization later.

## Decisions captured during execution

- CLI stays decoupled from `treadmill_api`: it posts a dict; the endpoint owns
  the schema + `recommend_mode`.

## Post-mortem

_(filled when the wave completes)_
