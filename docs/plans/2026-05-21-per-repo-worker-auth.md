---
auto_merge: false
status: active
---

# Plan: per-repo worker auth — mint scoped to the task's repo (ADR-0049 / ADR-0051 gating)

- **Status:** active
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0049 (App auth), ADR-0051 (operator-initiated bootstrap), ADR-0050
- **Related plans:** 2026-05-21-ramjac-bootstrap-smoke (this unblocks step 4)

## Goal

Make a worker mint a GitHub token **scoped to its task's repo**, so it can
operate on a repo other than the deployment's home (e.g. `RAMJAC/ramjac`).
Today the worker mints once at **startup** with **no repo** (before it knows the
task) — that yields the home installation's token (good enough for treadmill
work, the hotfix default), but a worker assigned a ramjac task gets a
*treadmill* token and can't push to ramjac. This is the gating fix for
ramjac worker tasks.

**Worker task, `auto_merge: false`:** this is the worker-auth path that just
caused an outage — the operator reviews the auth-timing change before merge.

## Success criteria

- After fetching the `WorkerContext` (repo known) and before cloning, a
  github+app-mode worker mints a token scoped to `ctx.repo` and re-applies it to
  `gh`, so subsequent `git`/`gh` calls act on the task's repo.
- The startup home-token bootstrap stays (the worker still boots); the per-task
  mint re-scopes on top of it.
- Endpoint unchanged — it already resolves `repo` (proven: it mints a
  ramjac-scoped token).

## Constraints / scope

### In scope
The single task below — generalize the App-token bootstrap to take an optional
`repo`, and call it per-task in the runner.

### Out of scope
The onboard endpoint/CLI (already merged); the discovery workflow; running the
smoke test. Token caching/refresh-mid-run is a later optimization.

## sequence_of_work

```yaml
sequence_of_work:
  - id: per-repo-worker-auth
    title: Worker mints a token scoped to the task's repo (ADR-0049)
    workflow: wf-author
    intent: |
      Make the github+app-mode worker re-mint its GitHub token scoped to the
      TASK's repo, after it knows the repo and before it clones. Read
      ``workers/agent/treadmill_agent/startup_auth.py`` and
      ``workers/agent/treadmill_agent/runner.py`` first.

      (1) In ``startup_auth.py``, GENERALIZE
      ``bootstrap_github_auth_via_app(*, settings)`` to accept an optional
      ``repo``: ``bootstrap_github_auth_via_app(*, settings, repo: str | None =
      None)``. When ``repo`` is set, POST ``{"repo": repo}`` to the
      installation-token endpoint instead of the current empty ``{}``; when
      ``None``, keep posting ``{}`` (the startup home-token bootstrap). Use
      ``json.dumps`` for the body. Keep applying the returned token to ``gh`` as
      today. Update the log line to mention the repo when present.

      (2) In ``runner.py`` ``_handle_step``: immediately AFTER the
      "fetched context" point where ``ctx`` is available (``ctx.repo`` is known —
      see the existing ``logger ... repo=%s`` line) and BEFORE ``_execute(ctx,
      settings)``, when ``settings.repo_mode == "github"`` and
      ``settings.github_auth_mode == "app"``, call
      ``startup_auth.bootstrap_github_auth_via_app(settings=settings,
      repo=ctx.repo)`` to re-scope ``gh`` to the task's repo. Wrap it so a mint
      failure fails the step cleanly (publish ``step.failed`` like other
      execution failures) rather than crashing the worker process — a per-task
      mint failure must not take the worker down (the outage lesson). Do not
      remove the startup bootstrap in ``__main__.py``.

      (3) TESTS:
        - In the startup_auth tests, assert that
          ``bootstrap_github_auth_via_app(settings=..., repo="o/n")`` POSTs a
          body containing ``"repo": "o/n"`` (patch ``urllib.request.urlopen``
          to capture the request + return a fake token JSON; assert the token is
          applied). Keep the existing no-repo (``{}``) behavior covered.
        - In the runner tests, assert ``_handle_step`` calls the per-repo mint
          with ``ctx.repo`` before ``_execute`` for an app-mode github worker
          (patch ``startup_auth.bootstrap_github_auth_via_app`` + ``_execute``;
          assert call order / args). Mirror the existing runner test setup.

      (4) DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``workers/agent/AGENT.md`` — note that app-mode workers re-mint a
      repo-scoped token per task (Key surfaces / Recent changes).

      Scope in the EXISTING test files for the modules you touch (startup_auth
      + runner tests) — the signature change + the new call may need them
      updated; don't leave the existing suite red.
    scope:
      files:
        - workers/agent/treadmill_agent/startup_auth.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_startup_auth.py
        - workers/agent/tests/test_runner.py
        - workers/agent/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/routers/github.py
        - workers/agent/treadmill_agent/__main__.py
    validation:
      - kind: deterministic
        description: |
          The bootstrap fn takes a repo, the runner re-mints per task, and the
          worker tests pass.
        script: |
          cd workers/agent \
            && grep -q "repo: str | None" treadmill_agent/startup_auth.py \
            && grep -q "bootstrap_github_auth_via_app(settings=settings, repo=ctx.repo)" treadmill_agent/runner.py \
            && uv run pytest tests/test_startup_auth.py tests/test_runner.py -q
```

## Risks / unknowns

- **The auth path that just broke** — hence `auto_merge: false` + operator
  review, and the per-task mint must fail the *step*, never the worker process.
- Existing runner/startup_auth tests may need updates for the signature + new
  call — scoped in.

## Decisions captured during execution

- Keep the startup home-token bootstrap; layer the per-task repo-scoped mint on
  top (minimal change; the worker still boots before it has a task).

## Post-mortem

_(filled when the wave completes)_
