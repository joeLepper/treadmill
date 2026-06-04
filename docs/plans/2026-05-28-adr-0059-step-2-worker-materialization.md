---
auto_merge: false
---

# Plan: ADR-0059 Step 2 — worker-side dep materialization

- **Status:** completed
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0059, ADR-0055 (per-account Claude credentials —
  the sibling per-step fetch shape this mirrors)
- **Supersedes:** none
- **Depends on:** ADR-0059 Step 1 (worker_deps schema + HTTP)

## Goal

Ship the worker-side materialization of per-repo `worker_deps` (ADR-0059
Step 2). Given a repo's `WorkerDeps` (Pydantic model from Step 1), the
worker builds a per-repo overlay (venv + node_modules + bin dir),
cached by `(repo, deps-hash)`, and activates the overlay before
invoking the task's gates.

Network egress scoping is **out of scope** — Step 3 formalizes that.
Step 2 materializes deps with whatever network access the worker
already has; the install phase will need egress for PyPI/npm/binary
mirrors. Step 3 will add per-phase iptables scoping. For now we
accept the wider trust surface during install.

`auto_merge: false` — same concurrent-orchestrator discipline.

## Success criteria

- `workers/agent/treadmill_agent/repo_deps.py` provides
  `materialize(repo: str, worker_deps: WorkerDeps) -> RepoOverlay`
  that:
  - Computes a deterministic `deps_hash` from the WorkerDeps shape.
  - Checks `/var/treadmill/repo-overlays/<repo-slug>/.deps-hash`; if
    present + matches, returns the cached `RepoOverlay` without
    re-installing.
  - Otherwise: creates `venv/`, runs `pip install <python>` into it;
    creates `node/` with `npm install --prefix` for `<node>`;
    downloads + sha256-verifies + chmods each binary into `bin/`.
  - Writes the deps-hash marker on success.
  - Raises `WorkerDepsMaterializationError` with a structured cause
    (stage: `python`/`node`/`binary`, detail) on failure.
- The runner (`workers/agent/treadmill_agent/runner.py` per-step seam,
  same neighborhood as `startup_auth.fetch_claude_credentials`)
  calls `materialize` after the App re-mint and before
  `_execute`. The returned `RepoOverlay` is bound to a `ContextVar`
  for the duration of the step.
- `RepoOverlay.env_overrides() -> dict[str, str]` returns PATH /
  PYTHONPATH / NODE_PATH augmentations the validation gate's
  subprocess uses. `validation_runtime.run_deterministic` reads
  the ContextVar and merges these into the subprocess env.
- Unit tests cover: hash determinism, cache-hit short-circuit,
  cache-miss materialization (with subprocess mocks), failure-mode
  propagation, env_overrides shape, empty-WorkerDeps short-circuit.

## Constraints / scope

### In scope

- `repo_deps.py` materialization logic + cache + hash + env_overrides.
- Runner hook to call `materialize` per-step before `_execute`.
- ContextVar plumbing into `validation_runtime.run_deterministic` so
  the subprocess env carries the overlay's PATH additions.
- Unit tests using `unittest.mock` for the `subprocess.run` calls
  (no real pip / npm / downloads in tests).
- AGENT.md update per ADR-0030.

### Out of scope

- Network egress scoping (Step 3).
- A new `task.worker_deps_failed` event (Step 4 — the architect's
  ADR-0058 gate-broken classifier already handles unrecognized-tool
  errors in the loop; failure event is for explicit operator
  telemetry).
- CLI surface for operator updates (Step 5).
- Integration smoke against real PyPI/npm (Step 6).
- Discovery / auto-detection from `requirements.txt` (separate plan).

### Budget

One worker dispatch. The task's scope is larger than Step 1 (more new
code; more subprocess interaction) — if the worker wedges or hits the
architect-amend cap, that's structural signal worth investigating.

## Sequence of work

```yaml
sequence_of_work:
  - id: adr-0059-step-2-worker-materialization
    title: "ADR-0059 Step 2 — repo_deps materialization + cache + runner hook"
    workflow: wf-author
    intent: |
      STUDY: read these as shape references —
        - `services/api/treadmill_api/models/onboarding.py` for the
          `WorkerDeps` + `BinarySpec` Pydantic models (Step 1).
        - `workers/agent/treadmill_agent/startup_auth.py` for the
          per-step fetch pattern (`fetch_claude_credentials` is the
          sibling shape — HTTP call, ContextVar binding, finally-
          reset).
        - `workers/agent/treadmill_agent/claude_code.py` `build_claude_env`
          for the env-merge pattern (mirrors what env_overrides will
          do for the validation subprocess).
        - `workers/agent/treadmill_agent/validation_runtime.py`
          `run_deterministic` for the subprocess.run signature where
          env_overrides will plug in.
        - `workers/agent/treadmill_agent/runner.py` `_handle_step` for
          where the materialization call lives — after the App
          re-mint, before `_execute`.

      BUILD:

      (1) New module `workers/agent/treadmill_agent/repo_deps.py` exporting:

          - `RepoOverlay` dataclass:
              * repo: str
              * deps_hash: str
              * venv_path: Path | None  (None when no python deps)
              * node_modules_path: Path | None  (None when no node deps)
              * bin_path: Path | None  (None when no binaries)
              * fresh: bool  (True if this materialize call did the work; False on cache hit)
              * def env_overrides(self) -> dict[str, str]:
                  Returns PATH (prepending venv/bin + bin_path + node_modules/.bin
                  to whatever is already in env), PYTHONPATH (overlay's site-
                  packages), NODE_PATH (overlay's node_modules). Empty dict
                  when all overlay paths are None.

          - `WorkerDepsMaterializationError(stage: str, detail: str)`
            exception.

          - `def compute_deps_hash(worker_deps: WorkerDeps) -> str`:
              Canonical serialization — sort each list, JSON-dump with
              sort_keys=True, sha256. Determinism over identical
              WorkerDeps must produce identical hashes.

          - `def materialize(repo: str, worker_deps: WorkerDeps,
              *, overlay_root: Path = Path("/var/treadmill/repo-overlays")) -> RepoOverlay`:
              - Returns RepoOverlay(fresh=False, ...) on cache hit
                (existing .deps-hash matches).
              - Empty WorkerDeps short-circuits to RepoOverlay(fresh=False,
                all paths None).
              - On cache miss: create overlay dirs, run installs (python →
                node → binaries in that order; each in its own try
                block raising WorkerDepsMaterializationError on failure),
                write .deps-hash, return RepoOverlay(fresh=True, ...).
              - All subprocess calls use `subprocess.run(..., check=True,
                capture_output=True, text=True, timeout=300)` so failures
                surface with stderr captured.
              - Binary downloads use `urllib.request.urlopen` (already
                in stdlib; no new dep). After download, compute sha256
                and compare against `BinarySpec.sha256_checksum`.
                Mismatch raises WorkerDepsMaterializationError(stage=
                "binary", detail="checksum mismatch ...").

      (2) ContextVar plumbing:

          - In `repo_deps.py`, add a module-level
            `_CURRENT_OVERLAY: ContextVar[RepoOverlay | None] =
            ContextVar('_repo_overlay', default=None)` and helpers
            `def bind_overlay(overlay: RepoOverlay) -> Token` and
            `def reset_overlay(token: Token) -> None`.

          - In `runner.py::_handle_step` (or the closest equivalent
            per-step seam — STUDY locates this), after the App
            re-mint and after `fetch_claude_credentials`:

              from treadmill_agent.repo_deps import materialize, bind_overlay, reset_overlay
              # Fetch worker_deps via the existing onboarding API.
              # For v1: extend `fetch_claude_credentials`'s sibling shape —
              # a new `fetch_repo_worker_deps(settings, repo)` helper in
              # startup_auth.py that GETs /api/v1/onboarding/repos/{repo}
              # and returns the WorkerDeps from the response. On 404 or
              # missing config, returns WorkerDeps() (empty — no
              # materialization needed).
              worker_deps = await fetch_repo_worker_deps(settings, ctx.repo)
              overlay = materialize(ctx.repo, worker_deps)
              token = bind_overlay(overlay)
              try:
                  await _execute(ctx)
              finally:
                  reset_overlay(token)

      (3) `validation_runtime.run_deterministic` env merge:

          - At the subprocess.run call, build env from:
              env = dict(os.environ)
              from treadmill_agent.repo_deps import current_overlay
              overlay = current_overlay()
              if overlay is not None:
                  for k, v in overlay.env_overrides().items():
                      env[k] = v
              # existing PR_NUMBER handling stays in place
          - Add `def current_overlay() -> RepoOverlay | None` to
            `repo_deps.py` that reads the ContextVar.

      (4) `startup_auth.py` extension:

          - New `async def fetch_repo_worker_deps(settings: Settings,
            repo: str) -> WorkerDeps`. Mirrors `fetch_claude_credentials`
            shape: GET /api/v1/onboarding/repos/{repo}, parse the
            response's `worker_deps` field as `WorkerDeps`, return.
            On 404 / 503 / network error, return `WorkerDeps()` —
            absence of config means no overlay (the legacy
            no-deps path stays in scope).

      TESTS in `workers/agent/tests/test_repo_deps.py`:

      - `test_compute_deps_hash_deterministic`: two identical WorkerDeps
        instances produce identical hashes; reordering input list elements
        also produces identical hashes (canonical sort).
      - `test_compute_deps_hash_differs_on_content`: changing a single
        package spec changes the hash.
      - `test_materialize_empty_worker_deps_short_circuits`: empty
        WorkerDeps returns RepoOverlay with all paths None, fresh=False,
        no subprocess calls.
      - `test_materialize_python_deps_calls_pip` (mock subprocess.run):
        WorkerDeps with python=["aws-cdk-lib==2.214.0"] triggers a
        `python -m venv` then `pip install` with the spec.
      - `test_materialize_cache_hit_short_circuits` (write a fake
        .deps-hash file matching what compute_deps_hash returns; assert
        no subprocess calls; fresh=False).
      - `test_materialize_python_install_failure_raises` (mock
        subprocess.run to raise CalledProcessError; assert
        WorkerDepsMaterializationError with stage='python').
      - `test_materialize_binary_checksum_mismatch_raises` (mock
        urlopen to return known bytes; pass a BinarySpec with the
        wrong sha256; assert stage='binary' + 'checksum mismatch').
      - `test_env_overrides_shape_with_python_only`,
        `test_env_overrides_shape_with_all_three`,
        `test_env_overrides_empty_when_no_overlay`.

      DOC: update `workers/agent/AGENT.md`:
        - Extend the validation_runtime / startup_auth key-surfaces
          lines to mention the new repo_deps module + env_overrides hook.
        - Add a Recent-changes entry citing ADR-0059 Step 2.

      Validation MUST NOT use `cdk synth`, `docker`, live AWS, or
      network egress. The new tests use unittest.mock for subprocess
      and urllib calls — no real installs or downloads.
    scope:
      files:
        - workers/agent/treadmill_agent/repo_deps.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/treadmill_agent/startup_auth.py
        - workers/agent/treadmill_agent/validation_runtime.py
        - workers/agent/tests/test_repo_deps.py
        - workers/agent/AGENT.md
      services_affected:
        - workers/agent
      out_of_scope:
        - Network egress scoping (ADR-0059 Step 3)
        - New event payloads (ADR-0059 Step 4)
        - CLI surface (ADR-0059 Step 5)
        - Live install integration test (ADR-0059 Step 6)
    validation:
      - kind: deterministic
        description: New repo_deps tests + extended startup_auth + validation_runtime tests stay green.
        script: |
          cd workers/agent && uv run pytest tests/test_repo_deps.py tests/test_runner_dispositions.py tests/test_claude_code.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: llm-judge
        description: AGENT.md updated per ADR-0030.
        prompt: |
          The DIFF should include AGENT.md updates in workers/agent/AGENT.md
          either extending the Key surfaces section to mention repo_deps.py
          OR adding a Recent-changes entry citing ADR-0059 Step 2. Return
          verdict 'pass' when present; 'fail' otherwise.
        severity: blocking
```

## Risks / unknowns

- **ContextVar across async boundaries.** The per-step seam in the
  runner runs inside an asyncio loop. ContextVar values DO propagate
  through `await` calls but NOT across thread boundaries by default.
  If `validation_runtime.run_deterministic` runs the subprocess on a
  thread executor (rather than `asyncio.create_subprocess_exec`), the
  ContextVar may not be visible. Worker should verify by reading the
  surrounding code; if the gap exists, pass the overlay explicitly to
  `run_deterministic` via a new kwarg as a fallback.
- **Fetch_repo_worker_deps on legacy repos.** Repos without a
  RepoConfig row return 404 today; the helper returns `WorkerDeps()`
  which materializes nothing. Existing behavior preserved.
- **Binary downloads need network egress.** Step 3 will scope this;
  for Step 2 we just call urllib and accept that the agent container
  needs egress during install. Add a clear log line so operators can
  see the egress when it happens.
- **We'll abort if** the runner's per-step seam turns out to be
  thread-bound (the ContextVar concern above) AND the fallback kwarg
  approach also doesn't fit cleanly. In that case bail to a sibling
  module-level `set_current_overlay()` / `clear_current_overlay()` and
  document the scoping carefully.

## Decisions captured during execution

(empty)

## Post-mortem

(filled in on completion / abandonment)
