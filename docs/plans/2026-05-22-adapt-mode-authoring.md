---
auto_merge: false
status: active
---

# Plan: adapt-mode authoring — doc API + CLI mirror + skill (ADR-0054)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0054 (this realizes it), ADR-0050 (S3 store / context-provider), ADR-0051 (operator-initiated bootstrap)

## Goal

Build ADR-0054: author ADRs/plans for an adapt repo (ramjac) against a **local
mirror synced over a REST doc API** backed by S3. Four steps — two dispatched to
workers (the doc API, the CLI), two hand-driven (the CDK bucket/IAM, the
user-level skill).

## Success criteria

- A per-deployment S3 bucket exists; the API's IAM can read/write it.
- `PUT/GET /api/v1/repos/{repo}/docs/{doc_path}` + `GET …/docs` persist + serve
  versioned docs via the merged `ContextStore` + `OnboardingStore`/`repo_context_docs`.
- `treadmill-local docs pull/push/list/get` round-trips a repo's docs to a local
  mirror.
- The mode-aware authoring skill, run in a repo session, materializes docs
  (adapt) → authors with file tools → pushes back; conform unchanged.
- End-to-end: from a ramjac session, author an ADR into the store and read it
  back via `pull`.

## Constraints / scope

### In scope
The two worker tasks below (doc API, CLI) + the two hand-driven steps (CDK
bucket/IAM, the skill).

### Out of scope
Conflict-merge beyond last-write-wins; learnings/rules doc kinds (ADRs+plans
first); wiring the adapt context-provider into the live role path (separate).

### Budget
One wave for the two worker tasks; the hand-driven steps proceed in parallel.

## Hand-driven steps (not dispatched — credential/infra + user-level)

- **Step 1 — CDK S3 bucket + API IAM** (operator/me): a per-deployment
  context-docs bucket; the API IAM user gets read/write **in the same policy
  change** (the ADR-0049 webhook-secret `AccessDenied` precedent — bucket in the
  policy up front). Adapter injects the bucket name into the API env
  (`CONTEXT_DOCS_BUCKET`). Sensitive (IAM + a CDK deploy) → hand-driven.
  **The bucket is REAL AWS S3, even for dev_local — never moto.** dev_local
  already runs against real AWS queues/secrets (ADR-0016); the docs are durable
  state, so they must persist in a real bucket and survive moto/container
  restarts. So `ContextStore` in dev_local uses a real boto3 S3 client against
  the CDK bucket (`AWS_ENDPOINT_URL` must NOT redirect S3 to moto for this).
- **Step 4 — the mode-aware authoring skill** (me): a user-level skill
  (`~/.claude/skills/`) so it loads in any target-repo session; orchestrates
  `docs pull` → author (reusing `/decide` `/plan`) → `docs push` for adapt,
  in-repo+commit for conform. Lives outside the treadmill repo → hand-driven.

The two worker tasks build + test with **mocked** deps, so they don't block on
step 1; integration (live `pull`/`push` against the bucket) happens once all
four land.

## sequence_of_work

```yaml
sequence_of_work:
  - id: doc-rest-api
    title: Context-doc REST API — PUT/GET/LIST repo docs (ADR-0054)
    workflow: wf-author
    intent: |
      Add a REST API for per-repo context docs, backed by the MERGED
      ``treadmill_api/context_store.py`` (``ContextStore`` — ``put_doc(repo,
      content)->key``, ``presigned_get_url(key)``) and
      ``treadmill_api/onboarding_store.py`` (``OnboardingStore.record_context_doc
      (session, repo, doc_path, s3_key, content_sha)->int``,
      ``get_context_doc(session, repo, doc_path)``). Read those + an existing
      router (``routers/github.py`` for the "service not configured -> 503"
      dependency pattern; ``routers/tasks.py`` for ``Depends(get_session)``).

      (1) Config: add ``context_docs_bucket: str | None = Field(default=None,
      alias="CONTEXT_DOCS_BUCKET")`` to ``treadmill_api/config.py`` (mirror the
      existing optional fields).

      (2) New router ``treadmill_api/routers/context_docs.py``
      (prefix ``/api/v1/repos``). A dependency ``get_context_store(request)``
      builds a ``ContextStore(boto3.client("s3", region_name=settings.aws_region),
      settings.context_docs_bucket)`` when the bucket is set, else raises 503
      (mirror github.py). Endpoints (``doc_path`` is a path param — use
      ``{doc_path:path}`` so slashes like ``adrs/0001-x.md`` work):
        - ``PUT /repos/{repo}/docs/{doc_path:path}`` body ``{"content": str}``:
          compute ``content_sha = hashlib.sha256(content.encode()).hexdigest()``;
          ``key = store.put_doc(repo, content)``; ``version = await
          OnboardingStore().record_context_doc(session, repo, doc_path, key,
          content_sha)``; ``await session.commit()``; return
          ``{repo, doc_path, version}``.
        - ``GET /repos/{repo}/docs/{doc_path:path}``:
          ``row = await OnboardingStore().get_context_doc(session, repo,
          doc_path)``; 404 if None; return ``{repo, doc_path, version: row.version,
          url: store.presigned_get_url(row.s3_key)}`` (the client fetches content
          from the presigned URL).
        - ``GET /repos/{repo}/docs``: query ``repo_context_docs`` for the latest
          version per ``doc_path`` for this repo (``DISTINCT ON (doc_path) …
          ORDER BY doc_path, version DESC`` or ``max(version) GROUP BY``); return
          ``{repo, docs: [{doc_path, version}]}``.

      (3) Register the router in ``treadmill_api/app.py`` (mirror an existing
      ``include_router`` + import).

      TESTS — NEW ``services/api/tests/test_context_docs_router.py`` using
      ``TestClient`` with ``app.dependency_overrides`` for ``get_session`` (stub
      session) and ``get_context_store`` (a fake/mock store), plus monkeypatching
      ``OnboardingStore`` methods (AsyncMock) — NO live DB/S3/network. Assert:
      PUT returns 200 with a version and calls put_doc + record_context_doc;
      GET returns the presigned url + version (404 when get_context_doc->None);
      LIST returns the docs; and a 503 when the bucket is unconfigured.

      DOCS (ADR-0030 — REQUIRED): update ``services/api/AGENT.md`` (Key surfaces
      + Recent changes). If editing ``app.py`` breaks an existing app/route test,
      that test file is in-scope to update.
    scope:
      files:
        - services/api/treadmill_api/routers/context_docs.py
        - services/api/treadmill_api/config.py
        - services/api/treadmill_api/app.py
        - services/api/tests/test_context_docs_router.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/context_store.py
        - services/api/treadmill_api/onboarding_store.py
        - services/api/treadmill_api/models/onboarding.py
    validation:
      - kind: deterministic
        description: |
          App imports with the router registered; the router tests pass.
        script: |
          cd services/api \
            && uv run python -c "import treadmill_api.app" \
            && grep -q "context_docs" treadmill_api/app.py \
            && uv run pytest tests/test_context_docs_router.py -q

  - id: cli-docs-commands
    title: treadmill-local docs — pull/push/list/get over the doc API (ADR-0054)
    workflow: wf-author
    intent: |
      Add the CLI happy-path over the doc REST API (the local-mirror bridge from
      ADR-0054). ADDITIVE to the ``treadmill-local`` CLI. Do NOT import
      ``treadmill_api`` — talk to the API over HTTP (stdlib ``urllib`` is fine,
      matching the worker's startup_auth pattern).

      NEW module ``tools/local-adapter/treadmill_local/docs_sync.py`` with pure,
      testable functions taking an injected ``api_url`` + an HTTP-get/put callable
      (so tests mock HTTP — NO network):
        - ``list_docs(api_url, repo) -> list[dict]`` — GET /api/v1/repos/{repo}/docs.
        - ``get_doc(api_url, repo, doc_path) -> str`` — GET the doc (returns the
          presigned ``url``), then fetch + return the content from that url.
        - ``pull(api_url, repo, dest: Path) -> list[str]`` — list, then for each
          doc write its content to ``dest/<doc_path>`` (mkdir parents); return the
          paths written.
        - ``push(api_url, repo, src: Path) -> list[tuple[str,int]]`` — for each
          file under ``src``, PUT /api/v1/repos/{repo}/docs/{relpath} with
          ``{"content": <file text>}``; return (doc_path, new_version) pairs.
          (v1 = push everything; last-write-wins per ADR-0054.)

      Then add a ``docs`` Typer command group to
      ``tools/local-adapter/treadmill_local/cli.py`` with ``list`` / ``get`` /
      ``pull`` / ``push`` subcommands. Options: ``--repo`` (default: infer from
      cwd git remote via the existing onboard helper if present, else required),
      ``--dir`` (mirror dir, default ``.treadmill-docs``), ``--api-url`` (default
      ``http://localhost:8088``).

      TESTS — NEW ``tools/local-adapter/tests/test_docs_sync.py``: unit-test
      ``list_docs``/``get_doc``/``pull``/``push`` with a fake HTTP layer + a
      ``tmp_path`` mirror (assert pull writes the right files; push PUTs each file
      with its content). NO network.

      DOCS (ADR-0030 — REQUIRED): update ``tools/local-adapter/AGENT.md``
      (``docs_sync.py`` + the ``docs`` command group). If adding the command
      breaks an existing cli test, that test file is in-scope.
    scope:
      files:
        - tools/local-adapter/treadmill_local/docs_sync.py
        - tools/local-adapter/treadmill_local/cli.py
        - tools/local-adapter/tests/test_docs_sync.py
        - tools/local-adapter/AGENT.md
      out_of_scope:
        - tools/local-adapter/treadmill_local/deployment_config.py
        - tools/local-adapter/treadmill_local/runtime.py
    validation:
      - kind: deterministic
        description: |
          The docs_sync module + CLI docs group exist and the unit tests pass.
        script: |
          cd tools/local-adapter \
            && grep -q "def pull" treadmill_local/docs_sync.py \
            && grep -q "docs" treadmill_local/cli.py \
            && uv run pytest tests/test_docs_sync.py -q
```

## Risks / unknowns

- **Shared files** (`app.py`, `config.py`, `cli.py`) could conflict with the
  sibling session — check before merging each (`auto_merge: false`).
- The doc API tests mock S3/DB; **runtime** needs step 1's bucket — integration
  test (`docs pull`/`push` live) comes after all four land.
- Presigned-URL GET means the CLI does a second hop to S3; fine for small docs.

## Decisions captured during execution

- GET returns a presigned URL (API stays thin, ADR-0050) rather than proxying
  content; the CLI fetches from the URL.

## Post-mortem

_(filled when the wave completes)_
