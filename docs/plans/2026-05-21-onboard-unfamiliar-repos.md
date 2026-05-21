---
auto_merge: true
status: active
---

# Plan: Onboard unfamiliar repos — foundational shapes (canary: ramjac)

- **Status:** active
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0050 (the onboarding architecture), ADR-0049 (auth substrate)

## Goal

Ship the foundational, additive data shapes for ADR-0050's unfamiliar-repo
onboarding — the repo config, the S3-backed external context store, and the
discovery profile + mode recommendation — built by Treadmill's own workers.
Each is a new module + tests that touches no live path and creates no schema,
so the wave is parallel-safe and dogfoods the pipeline. The chewy wiring stays
hand-driven. **ramjac** is the canary the finished capability is pointed at.

## Success criteria

Each task lands a PR that adds the named module + a passing named test, touches
only its scoped files, and wires nothing live:
- `repo_config.py` parses a per-repo config with a `mode` and an
  `auto_merge_blocked` flag, defaulting safely.
- `context_store.py` writes content-addressed docs to S3 and mints presigned
  URLs, against an injected S3 client (no network in tests).
- `repo_profile.py` carries the discovery schema and `recommend_mode()` returns
  `adapt` for context-rich repos, `conform` for sparse ones.

## Constraints / scope

### In scope
The three additive modules below, each building only on already-merged code,
each independent (no cross-imports between the three) so they merge in any order.

### Out of scope
Persistence migrations (index/profile/config tables), API router endpoints,
the `wf-discover` workflow + `role-cartographer`, wiring the context-provider
into the live role-context path, adapt-mode validator changes, the CDK S3
bucket + IAM, and the ramjac cutover. Those are hand-driven follow-ups once
these shapes land.

### Budget
One wave. If a unit can't be made additive-and-deterministic, drop it from the
wave rather than let it touch a live path.

## sequence_of_work

```yaml
sequence_of_work:
  - id: repo-config-model
    title: Per-repo config model (mode + auto-merge block)
    workflow: wf-author
    intent: |
      Create a NEW module ``services/api/treadmill_api/repo_config.py`` holding
      the per-repo onboarding config shape from ADR-0050 (decision 5). This is
      ADDITIVE — add no router, no DB model, no migration, and edit no other
      module. Persistence is a deliberate hand-driven follow-up.

      Define:
        - A frozen ``@dataclass RepoConfig`` with fields:
            ``repo: str``,
            ``mode: str = "conform"``  (the onboarding mode; only "conform" or
              "adapt" are valid),
            ``auto_merge_blocked: bool = False``  (when True, ALL auto-merge for
              this repo is blocked, independent of any plan-level flag),
            ``test_command: str | None = None``,
            ``lint_command: str | None = None``.
        - ``parse_repo_config(data: dict) -> RepoConfig`` — build a RepoConfig
          from a plain dict (e.g. parsed YAML/JSON). Apply the defaults above
          for missing keys. ``repo`` is required (raise ``ValueError`` if
          absent/empty). Raise ``ValueError`` if ``mode`` is present but not in
          ``{"conform", "adapt"}``. Coerce ``auto_merge_blocked`` to ``bool``.
        - ``to_dict(config: RepoConfig) -> dict`` — round-trips with
          ``parse_repo_config``.

      Then create a NEW test file
      ``services/api/tests/test_repo_config.py`` asserting:
        - ``parse_repo_config({"repo": "o/r"})`` yields mode "conform",
          ``auto_merge_blocked is False``, commands ``None``;
        - ``parse_repo_config({"repo": "o/r", "auto_merge_blocked": True})``
          sets the flag;
        - ``parse_repo_config({"repo": "o/r", "mode": "adapt"})`` keeps "adapt";
        - ``parse_repo_config({"repo": "o/r", "mode": "bogus"})`` raises
          ``ValueError``;
        - ``parse_repo_config({})`` (no repo) raises ``ValueError``;
        - ``to_dict(parse_repo_config(d)) == `` the normalized dict (round-trip).
    scope:
      files:
        - services/api/treadmill_api/repo_config.py
        - services/api/tests/test_repo_config.py
    validation:
      - kind: deterministic
        description: |
          The config module exists and the new test passes.
        script: |
          cd services/api \
            && grep -q "def parse_repo_config" treadmill_api/repo_config.py \
            && uv run pytest tests/test_repo_config.py -q

  - id: context-store-s3
    title: S3-backed content-addressed context store (presigned access)
    workflow: wf-author
    intent: |
      Create a NEW module ``services/api/treadmill_api/context_store.py``
      implementing the S3 blob side of ADR-0050's external context store
      (decision 4). Blobs go to S3 content-addressed; the Postgres index is a
      SEPARATE hand-driven follow-up — do NOT add a DB model or migration here.
      ADDITIVE — edit no other module.

      Define a class ``ContextStore``:
        - ``__init__(self, s3_client, bucket: str)`` — store the injected boto3
          S3 client and bucket name. Do NOT construct a client internally (so
          tests inject a mock).
        - ``put_doc(self, repo: str, content: str | bytes) -> str`` — compute
          ``sha256`` of the content (utf-8 encode if ``str``), form the key
          ``f"repo-context/{repo}/{sha}.md"``, call
          ``self._s3.put_object(Bucket=self._bucket, Key=key, Body=<bytes>)``,
          and return the key. Content-addressing means identical content yields
          the same key (idempotent).
        - ``presigned_get_url(self, key: str, expires_in: int = 3600) -> str``
          — return
          ``self._s3.generate_presigned_url("get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in)``.
        - ``presigned_put_url(self, key: str, expires_in: int = 3600) -> str``
          — same with ``"put_object"``.

      Then create a NEW test file
      ``services/api/tests/test_context_store.py`` using
      ``unittest.mock.MagicMock`` for the S3 client (NO network, NO moto
      dependency required):
        - ``put_doc("o/r", "hello")`` returns a key that starts with
          ``"repo-context/o/r/"`` and contains
          ``hashlib.sha256(b"hello").hexdigest()``; assert
          ``s3.put_object`` was called once with that Bucket/Key and
          ``Body == b"hello"``;
        - ``put_doc`` is idempotent: same content → same key on a second call;
        - ``presigned_get_url("k")`` returns the mock's
          ``generate_presigned_url`` return value and was called with
          ``"get_object"`` and the right Params.
    scope:
      files:
        - services/api/treadmill_api/context_store.py
        - services/api/tests/test_context_store.py
    validation:
      - kind: deterministic
        description: |
          The store module exists and the new test passes.
        script: |
          cd services/api \
            && grep -q "class ContextStore" treadmill_api/context_store.py \
            && uv run pytest tests/test_context_store.py -q

  - id: repo-profile-schema
    title: Discovery repo-profile schema + conform/adapt recommendation
    workflow: wf-author
    intent: |
      Create a NEW module ``services/api/treadmill_api/repo_profile.py`` holding
      the structured output of discovery (ADR-0050 decision 1) and the mode
      recommendation (decision 2). ADDITIVE — no DB, no router, no other module
      edited, and do NOT import from ``repo_config.py`` (keep this independent;
      the mode is a plain string "conform"/"adapt").

      Define:
        - A ``@dataclass RepoProfile`` with fields:
            ``repo: str``,
            ``languages: list[str]`` (default empty via ``field(default_factory=list)``),
            ``build_command: str | None = None``,
            ``test_command: str | None = None``,
            ``lint_command: str | None = None``,
            ``doc_paths: list[str]`` (default empty),
            ``components: list[str]`` (default empty),
            ``ci: str | None = None``,
            ``has_agent_context: bool = False``.
        - ``to_dict(profile) -> dict`` and ``from_dict(data: dict) -> RepoProfile``
          that round-trip (for JSONB persistence later).
        - ``recommend_mode(profile: RepoProfile) -> str`` returning the ADR-0050
          recommendation: ``"adapt"`` when the repo already carries its own
          discipline — i.e. ``profile.has_agent_context`` is True OR
          ``len(profile.doc_paths) >= 3`` — else ``"conform"``. Document the
          heuristic in the docstring; the operator confirms the final choice.

      Then create a NEW test file
      ``services/api/tests/test_repo_profile.py`` asserting:
        - ``from_dict(to_dict(p)) == p`` for a populated profile (round-trip);
        - defaults: ``RepoProfile(repo="o/r")`` has empty lists, ``None``
          commands, ``has_agent_context is False``;
        - ``recommend_mode`` returns "adapt" when ``has_agent_context=True``;
        - ``recommend_mode`` returns "adapt" when ``doc_paths`` has >= 3 entries;
        - ``recommend_mode`` returns "conform" for a sparse profile
          (no agent context, < 3 doc paths).
    scope:
      files:
        - services/api/treadmill_api/repo_profile.py
        - services/api/tests/test_repo_profile.py
    validation:
      - kind: deterministic
        description: |
          The profile module exists and the new test passes.
        script: |
          cd services/api \
            && grep -q "def recommend_mode" treadmill_api/repo_profile.py \
            && uv run pytest tests/test_repo_profile.py -q
```

## Diagram

See the onboarding sequence diagram in ADR-0050 — these three units build the
config, store, and profile shapes that diagram depends on.

## Risks / unknowns

- **Independence:** the three units must not import each other (none is merged
  yet). Scopes enforce it; the intents say so explicitly.
- **No migrations this wave:** persistence is deliberately deferred so parallel
  tasks can't collide on alembic heads (the ADR-0045 gate). The index/profile/
  config tables come hand-driven, in one migration, after these shapes land.
- **S3 in tests:** the store takes an injected client so tests mock it — no moto
  dependency, no network, matching the GitHub-App worker-task pattern.

## Decisions captured during execution

_(none yet)_

## Post-mortem

_(filled when the wave completes)_
