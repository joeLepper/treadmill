# Runbook — worker_deps canary (ADR-0059 step 6)

**Audience:** Treadmill operator who just landed an
``onboarding update`` against a repo's ``worker_deps`` (or
just rolled out an agent image / egress-proxy change that
touches the install seam) and wants to confirm the install
→ cache → task-phase path end-to-end before a real plan
exercises it.

**Scope:** Dev-local Docker stack. The same shape applies
in prod but the container-name + ``docker exec`` cadence
in the procedure assumes a local Docker daemon.

---

## TL;DR

```bash
treadmill onboarding update <repo> --worker-deps-python packaging==24.0       # 1. register
treadmill submit "noop canary" --repo <repo> --workflow wf-author --dev       # 2. dispatch
treadmill-local logs treadmill-worker-treadmill-agent-00001 -f                # 3. watch
treadmill submit "noop canary 2" --repo <repo> --workflow wf-author --dev     # 4. second pass
docker exec treadmill-worker-treadmill-agent-00001 \
  ls -l /var/treadmill/repo-overlays                                          # 5. verify overlay
```

Expected: step 3 shows ``repo_deps cache miss`` followed
by a ``pip install`` log; step 4 shows ``repo_deps cache
hit`` and *no* install log; step 5 shows
``<repo-slug>/.deps-hash`` alongside ``venv/``.

---

## Mental model

ADR-0059 carves the worker's lifecycle into two phases
with distinct network postures, both mediated by the
ADR-0060 sidecar HTTPS proxy:

| Phase | What runs | Egress reach |
|---|---|---|
| **Install** | ``repo_deps.materialize()`` — venv + ``pip install`` + ``npm install`` + signed-binary download | Install-phase allowlist (PyPI / npm / signed-URL hosts), reached via the credentialed proxy URL ``http://install:<token>@treadmill-egress-proxy:3128`` |
| **Task** | Everything else, including ``validation_runtime.run_deterministic`` | Always-allowed list only (no PyPI, no npm) |

The phase toggle is **proxy-side**: the credentialed URL
is the *only* signal that elevates a request to the
install-phase allowlist. Task-phase subprocesses are
intentionally left with the uncredentialed
``HTTPS_PROXY`` from the worker entrypoint — see the
absence assertion in
``workers/agent/tests/test_validation_runtime.py::
test_deterministic_subprocess_env_excludes_install_credential``.

The canary exercises both phases on the same worker:
phase 1 the first time the worker handles the repo
(install + ``.deps-hash`` write), phase 2 on every
subsequent task (cache hit, no install, no egress to a
registry).

Cross-references:

- [ADR-0059 — Per-repo worker-dep registration](../adrs/0059-per-repo-worker-deps-registration.md)
- [ADR-0060 — Sidecar HTTPS proxy for worker egress scoping](../adrs/0060-sidecar-https-proxy-for-worker-egress-scoping.md)

---

## Procedure

### 1. Verify the ADR-0060 egress proxy is up

The materialize step needs the install-phase allowlist
reachable. If the proxy is missing or its config-reload
hook lost the per-worker credential map, ``pip install``
will hang on a CONNECT that's silently refused.

```bash
docker exec treadmill-egress-proxy \
  python -c "import socket, sys; s=socket.socket(); s.settimeout(2); \
  s.connect(('127.0.0.1', 3128)); print('proxy listening on 3128'); s.close()"
```

Expected output: ``proxy listening on 3128``. If the
container isn't running, re-run ``treadmill-local up``
to reconcile.

### 2. Register canary worker_deps via the CLI

Use a tiny, stable, pure-Python pin so the install is
fast and the package itself doesn't pull surprising
transitive deps. ``packaging`` is the recommended
canary because the agent image already needs it
indirectly and the install completes in seconds.

```bash
treadmill onboarding update <repo> --worker-deps-python packaging==24.0
```

Confirm the update landed:

```bash
treadmill onboarding show <repo>
```

The output should list ``worker_deps.python``
containing ``packaging==24.0``.

### 3. Dispatch a no-op task against the canary repo

```bash
treadmill submit "worker_deps canary" \
  --repo <repo> --workflow wf-author --dev
```

``--dev`` flips the API into the local-only fast path
(per `cli/treadmill_cli/cli.py` ``submit`` D.10) so the
implicit one-task plan + wf-author task spawn in the
same transaction. The CLI prints ``plan=<id>
task=<id>``; note the task id for the next step.

### 4. Tail the worker container's stdout

```bash
treadmill-local logs treadmill-worker-treadmill-agent-00001 -f
```

The worker name uses the
``treadmill-worker-<family>-<nonce>`` convention from
``tools/local-adapter/treadmill_local/runtime.py`` —
substitute the actual nonce shown by
``docker ps --filter name=treadmill-worker``.

Expected log lines, in order:

  * ``repo_deps cache miss: repo=<repo> hash=<sha256>``
  * ``pip install`` output (or its captured stderr if
    the registry rate-limits) — captured by the
    ``subprocess.run(..., capture_output=True)`` in
    ``_install_python`` and surfaced only on failure.
    On success, no pip output reaches the log — the
    presence of the ``cache miss`` line plus the
    absence of a ``cache hit`` line is the signal.
  * Continuation into the normal step-execution log
    stream (Claude Code invocation, gh PR ops, etc.).

### 5. Re-dispatch the same task

```bash
treadmill submit "worker_deps canary 2" \
  --repo <repo> --workflow wf-author --dev
```

Expected this time:

  * ``repo_deps cache hit: repo=<repo> hash=<sha256>
    overlay=/var/treadmill/repo-overlays/<repo-slug>``
  * No ``pip install`` output, no cache-miss line.

A cache hit on the *second* dispatch but a miss on the
first is the load-bearing signal — it means the
``.deps-hash`` cache key is stable across worker
restarts (each worker exits after one step per
``EXIT_AFTER_STEP=true``; the overlay dir is bind-
mounted into every worker spawn).

### 6. Confirm the overlay layout on disk

```bash
docker exec treadmill-worker-treadmill-agent-00001 \
  ls -l /var/treadmill/repo-overlays
```

Expected: one directory per onboarded repo (slug =
``owner__name``) containing:

  * ``.deps-hash`` — the sha256 of the canonical
    ``WorkerDeps`` payload that produced the overlay
  * ``venv/`` — only if the repo registered any
    ``worker_deps.python`` specs
  * ``node_modules/`` — only if it registered any
    ``worker_deps.node`` specs

Binaries land under a sibling tree at
``/var/treadmill/repo-bin/`` (shared, not per-repo —
intentional; the binary registry is filename-keyed).

---

## Failure modes

### Materialize raised; step.failed but no operator escalation

If the worker stdout shows a Python traceback from
``repo_deps._install_python`` and the dashboard's
escalations surface shows only a generic
``step.failed``, the typed
``task.worker_deps_failed`` event did not publish.

Per ADR-0059 step 4 (see
``workers/agent/treadmill_agent/runner.py`` around
``_handle_step``'s materialize block), a
``WorkerDepsMaterializationError`` *must* trigger
``EventPublisher.publish_task_worker_deps_failed``
before re-raising — that's the operator-visible signal
that distinguishes a registration failure from
gate-broken / architect_cap / stuck_task_sweep. A
generic exception falls through without the typed
event by design. If the failure was a
``WorkerDepsMaterializationError`` and the typed event
didn't surface, the runner's wrapping is broken; pin
the regression with
``workers/agent/tests/test_runner.py::
test_handle_step_emits_worker_deps_failed_then_step_failed_on_materialize_error``
before patching.

### ``pip install`` hangs on CONNECT

The credentialed install URL didn't reach the proxy —
either ``TREADMILL_INSTALL_PROXY_TOKEN`` was not set on
the worker (autoscaler config drift; see
``tools/local-adapter/treadmill_local/egress_proxy.py``)
or the proxy's per-worker allowlist map didn't reload
after the worker spawned.

Diagnose by ``docker exec <worker> env | grep PROXY``:
``HTTPS_PROXY`` should be set; the install token's
presence is visible only inside ``materialize()``'s
subprocess env (per the ADR-0060 task-phase contract,
the credential is *deliberately* not in the parent
process env).

### ``checksum mismatch`` on a binary spec

The registration's ``sha256_checksum`` doesn't match
what the proxy fetched. Re-validate the spec with
``shasum -a 256 <downloaded-file>`` and update the
registration via ``treadmill onboarding update <repo>
--binary name=<url>=<correct-sha256>@<target>``. The
typed event will surface with ``stage='binary'`` —
that's the differentiator from python/node failures.

### Validation script can't find a registered package

The overlay was built but ``run_deterministic`` is
running with an env that's missing the overlay paths.
Most often this is a ContextVar-handoff regression:
``bind_overlay`` either wasn't called or
``current_overlay()`` returned ``None`` inside the
script seam. Pinned by
``workers/agent/tests/test_repo_deps_integration.py::
test_wiring_overlay_env_reaches_validation_subprocess``
— run that suite before touching anything else.

---

## Pointers

- [ADR-0059 — Per-repo worker-dep registration](../adrs/0059-per-repo-worker-deps-registration.md)
- [ADR-0060 — Sidecar HTTPS proxy for worker egress scoping](../adrs/0060-sidecar-https-proxy-for-worker-egress-scoping.md)
- `workers/agent/treadmill_agent/repo_deps.py` — the
  materialize + ContextVar surface this runbook canaries.
- `workers/agent/treadmill_agent/validation_runtime.py`
  — the task-phase seam that reads
  ``current_overlay().env_overrides()``.
- `workers/agent/tests/test_repo_deps_integration.py`
  — the wiring regression test that pins the
  ``repo_deps → validation_runtime`` ContextVar handoff
  for both the bound and the reset states.
- `workers/agent/tests/test_repo_deps.py` — the
  materialize-in-isolation unit suite this runbook is
  the operational counterpart to.
