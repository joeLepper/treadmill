# Plan: ADR-0065 — Real-Docker autoscaler smoke gate (implementation)

- **Status:** completed
- **Date:** 2026-06-02
- **Related ADRs:** ADR-0065 (the decision), ADR-0064 (the network
  topology the smoke validates), ADR-0060 (the original egress
  proxy that birthed this gap)

## Goal

Build the GitHub Actions smoke gate that runs a `treadmill-local
up`-style boot on real Docker and asserts a worker spawns and
mints a token. Make it required in branch protection for the
autoscaler-touching paths.

## Success criteria

1. `.github/workflows/autoscaler_smoke.yml` exists with the
   path-filter trigger from ADR-0065.
2. The smoke boots the dev-local stack on `ubuntu-latest` with
   `TREADMILL_EGRESS_PROXY_ENABLED=true`, spawns one worker, and
   asserts within 60 seconds:
   - the worker container is `Up`
   - the worker completed the installation-token mint
   - the worker can `curl http://treadmill-api:8088/healthz` from
     inside its network
   - (proxy-on) a CONNECT to an allowlisted host via `HTTPS_PROXY`
     succeeds
   - (proxy-on) a CONNECT to a non-allowlisted host returns 403
3. The GHA image cache makes warm-cache runs ≤45s; cold-cache ≤4
   minutes.
4. On failure, the workflow captures worker, proxy, and
   autoscaler logs as build artifacts.
5. Branch-protection rules require this gate for the filtered
   paths (operator action; documented below).
6. AGENT.md updates per ADR-0030 on `tools/local-adapter` (the
   smoke's wrapper script and path-filter rule) and on
   `services/egress-proxy` (the smoke covers it now).

## Constraints / scope

### In scope

- New workflow YAML.
- New helper script `tools/local-adapter/scripts/smoke_boot.sh`
  wrapping `treadmill-local up` with CI flags (no SSO, moto-backed,
  no observability, no scheduler).
- Shell + `docker exec` / `docker inspect` assertions; no test
  framework on the CI side.
- Log capture on failure.
- AGENT.md updates.
- The operator step of marking the gate REQUIRED in branch
  protection (manual; documented as the plan's closing step).

### Out of scope

- Generalizing the smoke to API, dashboard, or scheduler
  surfaces. The autoscaler surface is where the cascade happened;
  broadening lands in a follow-up if/when those surfaces show
  similar gaps.
- Real-AWS smoke. The dev-local Docker smoke is what catches the
  failure modes; cloud has its own deploy-watcher path.
- Removing existing tests. Unit tests + loopback-socket
  "integration" test stay; this gate is additive.

### Budget

Two worker dispatches.

## Sequence of work

```yaml
sequence_of_work:
  - id: autoscaler-smoke-boot-script
    title: "ADR-0065 Step 1 — smoke_boot.sh wrapper for CI boots"
    workflow: wf-author
    intent: |
      STUDY:
        - `tools/local-adapter/treadmill_local/cli.py` `up` command —
          flag surface for the boot procedure.
        - `tools/local-adapter/treadmill_local/runtime.py`
          `_up_dev_local` versus `up` — the dev-local boot reads
          a YAML; the fully-local boot uses moto. The CI smoke
          uses fully-local (moto) so it has no SSO dependency.
        - `tools/local-adapter/scripts/` for the directory layout
          convention for helper scripts (create if absent).

      BUILD: a new shell script
      `tools/local-adapter/scripts/smoke_boot.sh` that:
        - Parses `--port`, `--proxy-enabled`, `--timeout`
          flags. Defaults: port=8088, proxy-enabled=true,
          timeout=300.
        - Exports `TREADMILL_EGRESS_PROXY_ENABLED=$proxy_enabled`
          before invoking `treadmill-local up`.
        - Invokes `uv run treadmill-local up --no-build
          --no-autoscaler --no-scheduler --no-observability`
          (boot the minimal service set) — verify the CLI supports
          all those flags; add them to the CLI if any are missing.
        - Waits up to `$timeout` seconds for the API to respond
          200 on `http://localhost:$port/healthz`.
        - Spawns one worker via the autoscaler's
          `start_worker_once` entry point (via a small Python
          one-liner that imports the runtime and calls it).
        - Prints a structured `BOOT_READY` marker on success +
          `BOOT_FAILED` on timeout. Exit 0 / 1 respectively.

      Tests: `tools/local-adapter/tests/test_smoke_boot.py` covers
      the script's flag parsing only (the actual boot would need
      Docker which the worker sandbox doesn't have). Use a
      subprocess invocation of the script with `--help`-style
      flags + a fake-treadmill-local fixture to assert the script
      passes the right args downstream.

      AGENT.md update on `tools/local-adapter` referencing
      ADR-0065 and noting the script's purpose.
    scope:
      files:
        - tools/local-adapter/scripts/smoke_boot.sh
        - tools/local-adapter/tests/test_smoke_boot.py
        - tools/local-adapter/treadmill_local/cli.py
        - tools/local-adapter/AGENT.md
      services_affected:
        - tools/local-adapter
      out_of_scope:
        - .github/workflows/
    validation:
      - kind: deterministic
        description: |
          The flag-parsing tests pass.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_smoke_boot.py -q
      - kind: deterministic
        description: |
          The shell script is syntactically valid.
        script: |
          bash -n tools/local-adapter/scripts/smoke_boot.sh
      - kind: deterministic
        description: |
          The script invokes treadmill-local up and exposes the
          flags from the plan.
        script: |
          grep -lE "treadmill-local up" tools/local-adapter/scripts/smoke_boot.sh
          grep -lE "TREADMILL_EGRESS_PROXY_ENABLED" tools/local-adapter/scripts/smoke_boot.sh
      - kind: deterministic
        description: |
          AGENT.md references ADR-0065.
        script: |
          grep -lE "ADR-0065" tools/local-adapter/AGENT.md

  - id: autoscaler-smoke-workflow
    title: "ADR-0065 Step 2 — GitHub Actions workflow with path filter + assertions"
    workflow: wf-author
    depends_on: [task.autoscaler-smoke-boot-script.pr_merged]
    intent: |
      STUDY:
        - Existing GHA workflows under `.github/workflows/` for
          the project's conventions (image build, caching, secret
          handling, job structure).
        - `tools/local-adapter/scripts/smoke_boot.sh` from Step 1
          — the workflow invokes it.

      BUILD: new workflow `.github/workflows/autoscaler_smoke.yml`
      that:
        - Triggers on `pull_request` whose paths match the
          ADR-0065 filter (autoscaler, runtime, egress_proxy,
          docker_client, services/egress-proxy/**,
          workers/agent/Dockerfile,
          infra/treadmill_infra/stacks/**).
        - Runs on `ubuntu-latest` with `timeout-minutes: 15`.
        - Sets up docker buildx with a GHA cache layer keyed by
          the Dockerfiles' content hash so warm-cache runs reuse
          built images.
        - Builds the four required images via
          `_ensure_images_built` (or an equivalent shell sequence
          if simpler).
        - Invokes `tools/local-adapter/scripts/smoke_boot.sh`
          (from Step 1) with `--proxy-enabled true --timeout 300`.
        - After boot, runs the assertion block: `docker inspect`
          the worker; `docker exec` curl the API healthz; CONNECT
          to an allowlisted external host via HTTPS_PROXY; CONNECT
          to a non-allowlisted host and assert 403. The
          allowlisted host should be one of the
          ADR-0060 always-allowed set — `api.github.com` is
          cheapest to probe.
        - On step failure, uses `actions/upload-artifact` to
          capture the worker, proxy, and autoscaler logs (paths
          known from the boot script's structured output).
        - Includes a single retry on Docker daemon flakiness
          signatures (e.g. detect specific stderr patterns from
          docker daemon errors); pure assertion failures do NOT
          retry.

      ALSO: append a section to `tools/local-adapter/AGENT.md`
      naming the filtered paths + the rule "any new module under
      the autoscaler spawn surface must be added to
      autoscaler_smoke.yml's path filter." Append a "Recent
      changes" bullet on `services/egress-proxy/AGENT.md` noting
      the smoke now covers it.

      Operator-hand step (DOCUMENTED here, not a code change):
      After this PR merges, the operator goes to GitHub repo
      Settings → Branches → Branch protection rules for `main`
      and marks `autoscaler_smoke` as a required status check.
      That click-through cannot be done by a worker dispatch.
    scope:
      files:
        - .github/workflows/autoscaler_smoke.yml
        - .github/AGENT.md
        - tools/local-adapter/AGENT.md
        - services/egress-proxy/AGENT.md
      services_affected:
        - .github
        - tools/local-adapter
        - services/egress-proxy
      out_of_scope:
        - tools/local-adapter/scripts/
        - tools/local-adapter/treadmill_local/
    validation:
      - kind: deterministic
        description: |
          The workflow file is syntactically valid YAML.
        script: |
          uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/autoscaler_smoke.yml'))"
      - kind: deterministic
        description: |
          The workflow has the path filter + the boot script
          invocation + the assertion markers.
        script: |
          grep -lE "autoscaler\.py|egress_proxy\.py|docker_client\.py" .github/workflows/autoscaler_smoke.yml
          grep -lE "smoke_boot\.sh" .github/workflows/autoscaler_smoke.yml
          grep -lE "treadmill-api.*healthz|installation-token" .github/workflows/autoscaler_smoke.yml
      - kind: deterministic
        description: |
          AGENT.md files reference ADR-0065.
        script: |
          grep -lE "ADR-0065" tools/local-adapter/AGENT.md
          grep -lE "ADR-0065" services/egress-proxy/AGENT.md
```

## Diagram

Not applicable. ADR-0065 has the canonical sequenceDiagram for the
smoke's actor flow.

## Risks / unknowns

- **Docker-on-GHA flakiness.** The retry-once wrapper handles
  daemon-level flakes; assertion failures don't retry. We accept
  that some PRs will hit a flake and need a manual re-run.
- **Path-filter drift.** A new autoscaler module added without
  updating the filter stops firing the gate. Mitigation: the
  AGENT.md addition in Task 2 lists the filtered paths + the
  rule. A future ADR may grow this into a CI gate of its own
  (a CI-meta-gate); not in scope now.
- **GHA runner timeout.** A worst-case cold-cache build pushing
  past 15 minutes fails the whole workflow. Mitigation: the smoke
  uses minimal services (no observability, no scheduler) and
  caches aggressively. If a particular boot consistently exceeds
  the budget, we revisit the workflow's timeout config.
- **Branch-protection update is operator-hand.** The click-through
  to mark this gate REQUIRED happens in GitHub's web UI. Until
  the operator clicks, the gate runs but isn't load-bearing —
  PRs could merge red. Plan documents the step explicitly;
  operator carries it out as the closing action.

## Decisions captured during execution

(empty at draft time)

## Post-mortem

(filled when plan transitions to completed)
