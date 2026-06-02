# Plan: ADR-0064 — Multi-network attach for internal worker traffic (implementation)

- **Status:** drafting
- **Date:** 2026-06-02
- **Related ADRs:** ADR-0064 (the decision), ADR-0060 (the original
  egress-proxy design that ADR-0064 completes the topology for),
  ADR-0065 (the smoke gate that proves the topology works on real
  Docker)

## Goal

Ship the multi-network attach pattern so workers on `treadmill-egress`
can resolve `treadmill-api` and the egress proxy can forward to
external hosts. Flip the `TREADMILL_EGRESS_PROXY_ENABLED` default
from `false` back to `true` only after the topology change lands
and the ADR-0065 smoke gate proves it works on real Docker.

## Success criteria

1. The `treadmill-egress` Docker network is created during boot
   (in `_up_dev_local`) before any service spawns. The autoscaler's
   own `ensure_egress_network` call becomes idempotent (finds the
   network already present).
2. The `treadmill-api` container is attached to both
   `treadmill-local` (host port mapping) and `treadmill-egress`
   (worker DNS). The recreate path re-attaches to both.
3. The `treadmill-egress-proxy` container is attached to both
   `treadmill-egress` (worker-facing CONNECT) and `treadmill-local`
   (external gateway). Forwarded CONNECTs reach external hosts.
4. Workers stay on `treadmill-egress` only. They resolve
   `treadmill-api` via Docker DNS because the API multi-attaches.
5. With `TREADMILL_EGRESS_PROXY_ENABLED=true`, a real boot spawns
   a worker that mints its installation token within ten seconds.
   Unit tests assert the multi-attach on both the API and the
   proxy spawn paths.
6. AGENT.md updates per ADR-0030 on every touched component.

## Constraints / scope

### In scope

- Hoist `ensure_egress_network` to the boot path.
- Multi-attach the API + the proxy.
- New `connect_container_to_network` helper on the docker-client
  adapter so the multi-attach is a single call site with a fake
  for tests.
- Recreate-API path re-uses the same factory so deploy-watcher
  recreations also multi-attach.
- Default-flip of `TREADMILL_EGRESS_PROXY_ENABLED` (gated behind
  the smoke being green).
- AGENT.md updates.

### Out of scope

- Cloud (real-AWS) port of the network topology. ECS Security
  Groups and VPC subnets are different primitives; the cloud port
  is a separate ADR / plan if/when cloud needs the egress proxy.
- The smoke gate itself — that lives in ADR-0065 and its own plan.
- Removing the `TREADMILL_EGRESS_PROXY_ENABLED` feature flag. The
  flag survives as the operator escape hatch.
- Postgres / Redis / dashboard network changes. Those services
  stay on `treadmill-local` only — workers never address them.

### Budget

Three worker dispatches. If any task wedges at the architect cap,
investigate before the next ships.

## Sequence of work

```yaml
sequence_of_work:
  - id: ensure-egress-network-at-boot
    title: "ADR-0064 Step 1 — create treadmill-egress at boot, before services"
    workflow: wf-author
    intent: |
      STUDY:
        - `tools/local-adapter/treadmill_local/runtime.py` —
          `_up_dev_local` is the dev-local boot path. Find where
          `_ensure_network` (the existing helper for
          `treadmill-local`) is called and where `_start_services`
          fires. The new `ensure_egress_network` call goes before
          `_start_services` so the network exists when API spawns.
        - `tools/local-adapter/treadmill_local/autoscaler.py` —
          contains the existing `ensure_egress_network` call that
          becomes idempotent. Leave the call in place; it's still
          needed for the standalone-autoscaler test path.
        - `tools/local-adapter/treadmill_local/egress_proxy.py`
          `ensure_egress_network(adapter)` — confirm it's
          idempotent (it is, per the existing
          `adapter.ensure_network` which catches NotFound).

      BUILD:
        - In `runtime.py::_up_dev_local`, just before
          `self._start_services(...)`, add a call to
          `ensure_egress_network(adapter)` using a
          `DockerClientAdapter` constructed from the runtime's own
          `self.docker` client.
        - Verify the autoscaler's existing call is unchanged and
          still works (it should — `ensure_egress_network` is
          idempotent on the existing-network path).

      Tests:
        - In `tools/local-adapter/tests/test_runtime_dev_local.py`,
          assert that `_up_dev_local` triggers
          `ensure_egress_network` before any service spawn (mock
          the docker client; check call order).
        - In `tools/local-adapter/tests/test_autoscaler.py`, the
          existing `test_ensure_egress_network_creates_internal_network`
          continues to pass; add a coverage case where the
          network already exists (idempotent path).

      AGENT.md update on tools/local-adapter referencing ADR-0064.
    scope:
      files:
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/tests/test_autoscaler.py
        - tools/local-adapter/AGENT.md
      services_affected:
        - tools/local-adapter
      out_of_scope:
        - tools/local-adapter/treadmill_local/egress_proxy.py
    validation:
      - kind: deterministic
        description: |
          The runtime + autoscaler unit tests pass against the
          new boot-time network creation.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_runtime_dev_local.py tests/test_autoscaler.py -q
      - kind: deterministic
        description: |
          The new boot-time call to ensure_egress_network is
          present in _up_dev_local.
        script: |
          grep -lE "ensure_egress_network" tools/local-adapter/treadmill_local/runtime.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0064.
        script: |
          grep -lE "ADR-0064" tools/local-adapter/AGENT.md

  - id: multi-attach-api-and-proxy
    title: "ADR-0064 Step 2 — multi-attach API and proxy across networks"
    workflow: wf-author
    depends_on: [task.ensure-egress-network-at-boot.pr_merged]
    intent: |
      STUDY:
        - `tools/local-adapter/treadmill_local/docker_client.py`
          — current adapter shape. Add a sibling helper for
          attaching an already-running container to a second
          network.
        - `tools/local-adapter/treadmill_local/runtime.py` —
          where the API container is spawned in `_start_services`
          and recreated in `recreate_api_container`. Both sites
          need to multi-attach after creation.
        - `tools/local-adapter/treadmill_local/egress_proxy.py`
          `ensure_egress_proxy_container` — the proxy spawn site
          needs to multi-attach to `treadmill-local` after
          creation so its outbound CONNECTs route through that
          network's gateway.

      BUILD:
        - Add `connect_container_to_network(name: str, container)`
          to `DockerClientAdapter`. Implementation: get the
          network by name from the docker client, call
          `network.connect(container)`. Idempotent (skips when
          already attached).
        - In `runtime.py`, after the API container's
          `self.docker.containers.run(...)` call in
          `_start_services`, build a `DockerClientAdapter` and
          call `connect_container_to_network(EGRESS_NETWORK_NAME,
          api_container)`.
        - In `recreate_api_container`, after the new container is
          running, do the same multi-attach so deploy-watcher
          recreations don't drop the API off the egress network.
        - In `egress_proxy.py::ensure_egress_proxy_container`,
          after `adapter.run_container(...)` returns, call
          `adapter.connect_container_to_network(NETWORK_NAME,
          proxy_container)` where `NETWORK_NAME` is the
          `treadmill-local` constant from runtime.py (import it
          to avoid a magic-string drift).

      Tests:
        - `tools/local-adapter/tests/test_runtime.py` (or
          `test_runtime_dev_local.py` — pick the suite that
          already covers `_start_services`): assert
          `connect_container_to_network` is invoked with the
          egress network for the API spawn.
        - `tools/local-adapter/tests/test_autoscaler.py`: extend
          the existing
          `test_ensure_egress_proxy_container_spawns_when_not_running`
          to also assert the proxy is multi-attached to the local
          network.
        - Cover the recreate path:
          `tools/local-adapter/tests/test_runtime.py` (or the
          recreate-specific file): a case where
          `recreate_api_container` is called and the result
          re-attaches to both networks.

      AGENT.md updates on tools/local-adapter and
      services/egress-proxy referencing ADR-0064.
    scope:
      files:
        - tools/local-adapter/treadmill_local/docker_client.py
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/treadmill_local/egress_proxy.py
        - tools/local-adapter/tests/test_runtime.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/tests/test_autoscaler.py
        - tools/local-adapter/AGENT.md
        - services/egress-proxy/AGENT.md
      services_affected:
        - tools/local-adapter
        - services/egress-proxy
      out_of_scope:
        - tools/local-adapter/treadmill_local/deploy_watcher.py
    validation:
      - kind: deterministic
        description: |
          All affected test files pass.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_runtime.py tests/test_runtime_dev_local.py tests/test_autoscaler.py -q
      - kind: deterministic
        description: |
          The new helper is present on the adapter and called from
          both spawn sites.
        script: |
          grep -lE "connect_container_to_network" tools/local-adapter/treadmill_local/docker_client.py
          grep -lE "connect_container_to_network" tools/local-adapter/treadmill_local/runtime.py
          grep -lE "connect_container_to_network" tools/local-adapter/treadmill_local/egress_proxy.py
      - kind: deterministic
        description: |
          AGENT.md files reference ADR-0064.
        script: |
          grep -lE "ADR-0064" tools/local-adapter/AGENT.md
          grep -lE "ADR-0064" services/egress-proxy/AGENT.md

  - id: flip-egress-proxy-default-on
    title: "ADR-0064 Step 3 — flip TREADMILL_EGRESS_PROXY_ENABLED default to true"
    workflow: wf-author
    depends_on: [task.multi-attach-api-and-proxy.pr_merged]
    intent: |
      STUDY:
        - `tools/local-adapter/treadmill_local/autoscaler.py`
          where `egress_proxy_enabled` is read from
          `TREADMILL_EGRESS_PROXY_ENABLED`. The default is
          currently `"false"`; flip to `"true"`.
        - The PR description must record that the ADR-0065 smoke
          gate was green on a real boot before this task ships;
          the dev-local operator runs the smoke locally as part
          of merging this step. The architect's review payload
          should reflect the smoke result.

      BUILD:
        - Change the default string in the env read from `"false"`
          to `"true"`. Update the inline comment above the read
          to describe the new default + the conditions under which
          an operator would flip it back off (debugging the
          egress proxy itself, suspected proxy bug, etc.).
        - Update the autoscaler test for `egress_proxy_enabled`
          to reflect the new default (the existing flag test
          should still pass with one assertion flipped).

      AGENT.md update on tools/local-adapter: note the flag's
      new default plus its continued existence as escape hatch.
    scope:
      files:
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/tests/test_autoscaler.py
        - tools/local-adapter/AGENT.md
      services_affected:
        - tools/local-adapter
      out_of_scope:
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/treadmill_local/egress_proxy.py
    validation:
      - kind: deterministic
        description: |
          The autoscaler tests pass with the flipped default.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_autoscaler.py -q
      - kind: deterministic
        description: |
          The default in the env-read is now true.
        script: |
          grep -lE "TREADMILL_EGRESS_PROXY_ENABLED" tools/local-adapter/treadmill_local/autoscaler.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0064 and the flag-default change.
        script: |
          grep -lE "ADR-0064" tools/local-adapter/AGENT.md
          grep -lE "TREADMILL_EGRESS_PROXY_ENABLED" tools/local-adapter/AGENT.md
```

## Diagram

Not applicable. ADR-0064 has the canonical network-topology
flowchart that this plan implements.

## Risks / unknowns

- **Task 2's recreate-path coverage.** The deploy-watcher's
  `recreate_api_container` is the auto-deploy entry on
  `services/api/**` merges. If multi-attach is omitted there,
  every auto-deploy will drop the API off `treadmill-egress`
  and re-introduce the cross-network DNS bug. Mitigation: the
  multi-attach lives in the factory function (`_build_api_service_spec`
  or the spawn helper); both spawn and recreate call the same
  helper. Test coverage in Task 2 specifically exercises the
  recreate path.
- **Task 3 flipping default-on without Task 2 actually working.**
  If the multi-attach has any latent bug, flipping the default
  re-introduces the cascade. Mitigation: the `depends_on` chain
  serializes the tasks, plus the operator runs the ADR-0065 smoke
  gate locally before merging Task 3's PR. The PR description
  carries the smoke result.
- **Docker network-attach race during parallel boots.** Two
  `treadmill-local up` invocations against the same engine could
  race the `network.connect` call. Mitigation: the helper is
  idempotent (skips when already attached); concurrent runs
  produce identical end state.
- **The cloud-side ECS port is non-trivial.** When we eventually
  run the egress proxy in cloud, the multi-network design here
  doesn't translate directly. Mitigation: explicitly out of scope;
  cloud port gets its own ADR.

## Decisions captured during execution

(empty at draft time)

## Post-mortem

(filled when plan transitions to completed)
