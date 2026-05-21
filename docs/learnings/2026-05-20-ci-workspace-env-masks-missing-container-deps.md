---
date: 2026-05-20
trigger: surprise
status: captured
related: ADR-0049, 2026-05-17-alembic-heads-ci-gate
---

# Learning: CI's uv-workspace env masks missing container runtime deps

## Trigger

Activating the GitHub App (ADR-0049 phase 2), the API container crashed at
startup:

```
File "/app/treadmill_api/github_app.py", line 23
    from cryptography.hazmat.primitives import hashes, serialization
ModuleNotFoundError: No module named 'cryptography'
```

`github_app.py` (merged in PR #206) imports `cryptography`; PR #211 wired it
into `app.py`'s import chain. **Both PRs passed CI and merged green.** The break
only appeared when a fresh image was built and the import actually executed.

## Observation

`cryptography` was never declared in `services/api/pyproject.toml`. It was
present *transitively* in the uv **workspace** (pulled by a sibling member), so
local `uv run` and CI `uv sync` both had it on the path. But the API container
image builds via `pip install .` — `services/api`'s **declared, direct** deps
only — and that closure had no `cryptography`. So the module imported fine
everywhere the workspace env was active (dev, CI) and only failed in the
container, which is the actual deploy artifact.

It stayed latent from #206 to #211 because the module was *additive and
unimported* — nothing in the live import chain referenced `github_app` until
#211 wired `app.py → github_auth → github_app`. Additive modules hide this gap
the longest: the import that triggers the crash isn't executed until the code is
finally used.

## Generalization

In a uv (or any) workspace, a member can `import` a package that is only
transitively present via a sibling, and **both local dev and CI (`uv sync`) will
have it** — while a per-member artifact built from that member's *declared* deps
(a Docker image via `pip install .`, a wheel) will not. The test environment
diverges from the deploy artifact, and the divergence is invisible until the
import runs at runtime in the artifact. This is the same shape as the
`alembic-heads-ci-gate` gap: **CI green is not deploy-safe when CI's env is a
superset of the artifact's env.**

## Proposed rule

Every package a service imports MUST be a declared dependency in that service's
own `pyproject.toml`. Never rely on transitive presence from a workspace
sibling. Adding an import is adding a dependency.

## Proposed remediation

- **Deterministic (preferred):** a CI step that builds the service image and
  smoke-imports the app (`python -c "import treadmill_api.app"`), so a missing
  *direct* dep fails CI the way it would fail the container. Cheap, and it
  closes the whole class — pairs naturally with the still-open
  `alembic-heads-ci-gate` work (both are "CI env ≠ deploy artifact" gaps).
- **Lighter:** an import-linter / deptry-style check that flags imports not in
  the declared deps.
- **LLM-judge:** reviewer checks each new top-level import in a PR against the
  service's declared dependencies.

## Notes

- Cost this time: a window where `main` produced a crash-on-startup API image,
  and a failed first activation attempt. Fixed in PR #212 (declare
  `cryptography>=43.0.0`).
- Compounding factor: `treadmill-local up` is start-if-not-running, so the first
  redeploy didn't even recreate the container to surface the crash — the missing
  dep only appeared once the container was force-recreated. Two separate "the
  thing I changed didn't take effect the way I assumed" surprises in one
  activation.
