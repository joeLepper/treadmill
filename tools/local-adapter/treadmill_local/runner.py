"""Resolve ECS task definitions and services from CFN into specs the runtime
can start as native Docker containers on the Treadmill local network.

Two shapes:

  ContainerSpec  — one container per task definition + container-definition
                   pair. Used by the autoscaler to launch worker replicas.
  ServiceSpec    — one entry per non-autoscaled ECS Service. Carries the
                   underlying ContainerSpec(s) plus desired count and
                   port mappings for host-side access.

Workers (autoscaled services) are skipped by `resolve_services` because the
autoscaler subprocess manages their lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from treadmill_local.synth import CFNResource, SynthResult, resolve_value


# When containers run on the Treadmill local network, they reach moto by
# container name on its internal port — not the host-mapped port. The runtime
# passes these in so we don't hardcode them here.
@dataclass
class LocalNetworkConfig:
    network_name: str
    moto_container_name: str
    moto_internal_port: int = 5000
    moto_host_port: int = 5001
    region: str = "us-east-1"
    fake_access_key: str = "test"
    fake_secret_key: str = "test"

    @property
    def moto_endpoint(self) -> str:
        return f"http://{self.moto_container_name}:{self.moto_internal_port}"

    @property
    def moto_host_endpoint(self) -> str:
        """The host-visible moto URL — the form that the provisioner's
        SQS / SNS responses embed (because moto returns its own external
        host). Used to rewrite resolved env values so containers reach
        moto via the container-network hostname instead of ``localhost``."""
        return f"http://localhost:{self.moto_host_port}"


@dataclass
class ContainerSpec:
    """A resolved spec for one container the adapter knows how to start."""

    family: str
    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    network: str = ""
    container_ports: list[int] = field(default_factory=list)


@dataclass
class ServiceSpec:
    """A non-autoscaled ECS Service to be started as one or more long-running
    Docker containers at `treadmill local up`."""

    family: str
    desired_count: int
    container_specs: list[ContainerSpec]
    port_mappings: list[tuple[int, int]] = field(default_factory=list)
    """List of (container_port, host_port) tuples for host-side access."""


# Common dev-port collisions: shift these to non-default host ports so a
# developer's existing local services aren't trampled. Other ports map
# 1:1 to the host. Future ADR generalizes this via per-service config.
_DEFAULT_HOST_PORT_OVERRIDES: dict[int, int] = {
    5432: 15432,  # Postgres
    6379: 16379,  # Redis
    3306: 13306,  # MySQL
}


def _default_host_port(container_port: int) -> int:
    """Map a container port to the default host port for local exposure."""
    return _DEFAULT_HOST_PORT_OVERRIDES.get(container_port, container_port)


# ── Task definition resolution ────────────────────────────────────────────────


def resolve_task_definitions(
    synth: SynthResult,
    refs: dict[str, str],
    network: LocalNetworkConfig,
) -> list[ContainerSpec]:
    """Walk AWS::ECS::TaskDefinition resources and produce ContainerSpec list.

    For each container in each task def:
      * Resolve Image (literal string in nearly all real-world CDK).
      * Resolve each Environment[*].Value via *refs* (Ref / Fn::GetAtt / Fn::Join).
      * Extract container ports from the PortMappings array.
      * Augment env with locally-required values (AWS_ENDPOINT_URL pointed at
        moto's container-network address, fake credentials, region) so the
        container reaches moto on the docker network.
    """
    specs: list[ContainerSpec] = []
    for res in synth.by_type("AWS::ECS::TaskDefinition"):
        family = res.properties.get("Family") or res.logical_id
        for cd in res.properties.get("ContainerDefinitions", []):
            specs.append(_container_spec(cd, family, refs, network))
    return specs


def _container_spec(
    cd: dict,
    family: str,
    refs: dict[str, str],
    network: LocalNetworkConfig,
) -> ContainerSpec:
    name = cd.get("Name") or family
    image = resolve_value(cd.get("Image"), refs) or ""

    env: dict[str, str] = {}
    for kv in cd.get("Environment", []) or []:
        k = resolve_value(kv.get("Name"), refs)
        v = resolve_value(kv.get("Value"), refs)
        if k and v is not None:
            env[k] = v

    # Provisioner refs from moto embed the host-visible URL (``localhost``);
    # rewrite to the docker-network hostname so containers can actually
    # reach moto. Without this, env vars like ``EVENTS_QUEUE_URL`` look
    # right from the host but break inside the container.
    host_prefix = network.moto_host_endpoint
    container_prefix = network.moto_endpoint
    for k, v in list(env.items()):
        if isinstance(v, str) and v.startswith(host_prefix):
            env[k] = v.replace(host_prefix, container_prefix, 1)

    # Augment with locally-required values. Existing entries win — we don't
    # overwrite anything the CDK author set explicitly.
    locals_ = {
        "AWS_ENDPOINT_URL": network.moto_endpoint,
        "AWS_DEFAULT_REGION": network.region,
        "AWS_REGION": network.region,
        "AWS_ACCESS_KEY_ID": network.fake_access_key,
        "AWS_SECRET_ACCESS_KEY": network.fake_secret_key,
    }
    for k, v in locals_.items():
        env.setdefault(k, v)

    container_ports: list[int] = []
    for pm in cd.get("PortMappings", []) or []:
        cp = pm.get("ContainerPort")
        if isinstance(cp, int):
            container_ports.append(cp)

    return ContainerSpec(
        family=family,
        name=name,
        image=image,
        env=env,
        network=network.network_name,
        container_ports=container_ports,
    )


def find_spec(specs: list[ContainerSpec], family: str) -> ContainerSpec:
    """Look up a single spec by family name. Raises if not found."""
    matches = [s for s in specs if s.family == family]
    if not matches:
        raise ValueError(f"No task definition with family {family!r}")
    if len(matches) > 1:
        raise NotImplementedError(
            f"Family {family!r} has {len(matches)} containers; multi-container "
            "task definitions not yet supported by the spike adapter."
        )
    return matches[0]


# ── Service resolution ────────────────────────────────────────────────────────


def autoscaled_service_logical_ids(synth: SynthResult) -> set[str]:
    """Return the set of ECS Service logical IDs targeted by an autoscaling
    target in the stack. These services are managed by the autoscaler
    subprocess, not started at `up`."""
    services = {s.logical_id for s in synth.by_type("AWS::ECS::Service")}
    autoscaled: set[str] = set()
    for st in synth.by_type("AWS::ApplicationAutoScaling::ScalableTarget"):
        autoscaled |= _service_refs_in(st.properties, services)
    return autoscaled


def _service_refs_in(value: Any, service_logical_ids: set[str]) -> set[str]:
    """Walk a CFN value recursively, returning service logical IDs that
    appear inside Ref or Fn::GetAtt nodes."""
    found: set[str] = set()
    if isinstance(value, dict):
        if "Ref" in value and value["Ref"] in service_logical_ids:
            found.add(value["Ref"])
        if "Fn::GetAtt" in value:
            ga = value["Fn::GetAtt"]
            if isinstance(ga, list) and ga and ga[0] in service_logical_ids:
                found.add(ga[0])
        for v in value.values():
            found |= _service_refs_in(v, service_logical_ids)
    elif isinstance(value, list):
        for item in value:
            found |= _service_refs_in(item, service_logical_ids)
    return found


def resolve_services(
    synth: SynthResult,
    refs: dict[str, str],
    network: LocalNetworkConfig,
) -> list[ServiceSpec]:
    """Return ServiceSpec for each non-autoscaled ECS Service.

    Each ServiceSpec carries the desired_count and the ContainerSpec(s)
    derived from the referenced TaskDefinition. Port mappings are
    constructed using the default host-port overrides.
    """
    autoscaled = autoscaled_service_logical_ids(synth)
    task_defs_by_logical_id: dict[str, CFNResource] = {
        td.logical_id: td for td in synth.by_type("AWS::ECS::TaskDefinition")
    }

    specs: list[ServiceSpec] = []
    for svc in synth.by_type("AWS::ECS::Service"):
        if svc.logical_id in autoscaled:
            continue

        td_ref = svc.properties.get("TaskDefinition")
        if not isinstance(td_ref, dict) or "Ref" not in td_ref:
            continue
        td = task_defs_by_logical_id.get(td_ref["Ref"])
        if td is None:
            continue

        family = td.properties.get("Family") or td_ref["Ref"]
        desired = int(svc.properties.get("DesiredCount", 1))

        container_specs: list[ContainerSpec] = []
        port_mappings: list[tuple[int, int]] = []
        for cd in td.properties.get("ContainerDefinitions", []) or []:
            cspec = _container_spec(cd, family, refs, network)
            container_specs.append(cspec)
            for cp in cspec.container_ports:
                port_mappings.append((cp, _default_host_port(cp)))

        specs.append(
            ServiceSpec(
                family=family,
                desired_count=desired,
                container_specs=container_specs,
                port_mappings=port_mappings,
            )
        )
    return specs
