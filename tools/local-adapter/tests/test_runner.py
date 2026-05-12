"""Unit tests for the runner module — task definition resolution and env wiring."""

from __future__ import annotations

import pytest

from treadmill_local.runner import (
    ContainerSpec,
    LocalNetworkConfig,
    autoscaled_service_logical_ids,
    find_spec,
    resolve_services,
    resolve_task_definitions,
)
from treadmill_local.synth import CFNResource, SynthResult


def _result(*resources: CFNResource) -> SynthResult:
    return SynthResult(
        stack_name="Test",
        template_path=None,  # type: ignore[arg-type]
        template={},
        resources=list(resources),
    )


def _network() -> LocalNetworkConfig:
    return LocalNetworkConfig(
        network_name="test-net",
        moto_container_name="test-moto",
    )


def test_resolve_task_definition_basic_env():
    """The runner rewrites moto's ``localhost`` URLs to their container-
    network form so containers on the bridge network actually reach moto."""
    refs = {"WorkQueue": "http://localhost:5001/123/work.fifo"}
    task_def = CFNResource(
        "WorkerTask",
        "AWS::ECS::TaskDefinition",
        {
            "Family": "noop-worker",
            "ContainerDefinitions": [
                {
                    "Name": "noop",
                    "Image": "noop-worker:dev",
                    "Environment": [
                        {"Name": "SQS_QUEUE_URL", "Value": {"Ref": "WorkQueue"}},
                        {"Name": "EXIT_AFTER_STEP", "Value": "true"},
                    ],
                },
            ],
        },
    )
    [spec] = resolve_task_definitions(_result(task_def), refs, _network())
    assert spec.family == "noop-worker"
    assert spec.name == "noop"
    assert spec.image == "noop-worker:dev"
    # Localhost rewritten to the container-network hostname.
    assert spec.env["SQS_QUEUE_URL"] == "http://test-moto:5000/123/work.fifo"
    assert spec.env["EXIT_AFTER_STEP"] == "true"
    assert spec.network == "test-net"


def test_resolve_does_not_rewrite_unrelated_localhost_values():
    """Only URLs that match the moto host endpoint are rewritten. A value
    that happens to contain ``localhost`` for a different reason (e.g. a
    Postgres DSN pointing at the in-network postgres host) is left alone."""
    refs = {}
    task_def = CFNResource(
        "T", "AWS::ECS::TaskDefinition",
        {
            "Family": "f",
            "ContainerDefinitions": [{
                "Name": "c", "Image": "i",
                "Environment": [
                    {"Name": "DATABASE_URL", "Value": "postgresql://x@treadmill-postgres:5432/y"},
                    {"Name": "OTHER", "Value": "http://localhost:9999/unrelated"},
                ],
            }],
        },
    )
    network = LocalNetworkConfig(
        network_name="test-net",
        moto_container_name="test-moto",
        moto_host_port=5001,
    )
    [spec] = resolve_task_definitions(_result(task_def), refs, network)
    assert spec.env["DATABASE_URL"] == "postgresql://x@treadmill-postgres:5432/y"
    # 9999 ≠ 5001 → not a moto URL → not rewritten.
    assert spec.env["OTHER"] == "http://localhost:9999/unrelated"


def test_resolve_augments_with_aws_endpoint_url():
    """Workers running on the docker network reach moto by container name on
    its internal port — not the host-mapped port. The runner must inject
    AWS_ENDPOINT_URL pointing at the container-network address."""
    task_def = CFNResource(
        "T", "AWS::ECS::TaskDefinition",
        {"Family": "f", "ContainerDefinitions": [{"Name": "c", "Image": "i"}]},
    )
    [spec] = resolve_task_definitions(_result(task_def), {}, _network())
    assert spec.env["AWS_ENDPOINT_URL"] == "http://test-moto:5000"
    assert spec.env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert spec.env["AWS_REGION"] == "us-east-1"
    assert spec.env["AWS_ACCESS_KEY_ID"] == "test"
    assert spec.env["AWS_SECRET_ACCESS_KEY"] == "test"


def test_user_env_wins_over_locally_augmented_defaults():
    """If the CDK author set AWS_DEFAULT_REGION explicitly, we don't overwrite it."""
    task_def = CFNResource(
        "T", "AWS::ECS::TaskDefinition",
        {
            "Family": "f",
            "ContainerDefinitions": [{
                "Name": "c", "Image": "i",
                "Environment": [{"Name": "AWS_DEFAULT_REGION", "Value": "eu-west-1"}],
            }],
        },
    )
    [spec] = resolve_task_definitions(_result(task_def), {}, _network())
    assert spec.env["AWS_DEFAULT_REGION"] == "eu-west-1"


def test_resolve_skips_unresolvable_env_value():
    """Env entries whose Value can't be resolved are dropped, not crashed on."""
    task_def = CFNResource(
        "T", "AWS::ECS::TaskDefinition",
        {
            "Family": "f",
            "ContainerDefinitions": [{
                "Name": "c", "Image": "i",
                "Environment": [
                    {"Name": "MISSING", "Value": {"Ref": "DoesNotExist"}},
                    {"Name": "OK", "Value": "literal"},
                ],
            }],
        },
    )
    [spec] = resolve_task_definitions(_result(task_def), {}, _network())
    assert "MISSING" not in spec.env
    assert spec.env["OK"] == "literal"


def test_resolve_falls_back_to_logical_id_when_family_missing():
    task_def = CFNResource(
        "MyTaskDef", "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [{"Name": "c", "Image": "i"}]},
    )
    [spec] = resolve_task_definitions(_result(task_def), {}, _network())
    assert spec.family == "MyTaskDef"


def test_find_spec_raises_on_missing():
    specs = [ContainerSpec(family="a", name="a", image="a")]
    with pytest.raises(ValueError, match="No task definition with family 'b'"):
        find_spec(specs, "b")


def test_find_spec_raises_on_multi_container_family():
    """The spike adapter doesn't support task defs with multiple containers."""
    specs = [
        ContainerSpec(family="x", name="c1", image="i"),
        ContainerSpec(family="x", name="c2", image="i"),
    ]
    with pytest.raises(NotImplementedError, match="multi-container"):
        find_spec(specs, "x")


def test_resolve_handles_no_task_definitions():
    """A stack without ECS task defs produces no specs."""
    assert resolve_task_definitions(_result(), {}, _network()) == []


def test_resolve_extracts_container_ports_from_port_mappings():
    task_def = CFNResource(
        "T", "AWS::ECS::TaskDefinition",
        {
            "Family": "api",
            "ContainerDefinitions": [{
                "Name": "api", "Image": "treadmill-api:dev",
                "PortMappings": [
                    {"ContainerPort": 8088, "Protocol": "tcp"},
                ],
            }],
        },
    )
    [spec] = resolve_task_definitions(_result(task_def), {}, _network())
    assert spec.container_ports == [8088]


def test_resolve_handles_missing_port_mappings():
    """Tasks without PortMappings (workers) get an empty container_ports list."""
    task_def = CFNResource(
        "T", "AWS::ECS::TaskDefinition",
        {"Family": "f", "ContainerDefinitions": [{"Name": "c", "Image": "i"}]},
    )
    [spec] = resolve_task_definitions(_result(task_def), {}, _network())
    assert spec.container_ports == []


# ── Service resolution ────────────────────────────────────────────────────────


def _service(logical_id: str, task_def_logical_id: str, name: str, desired: int = 1) -> CFNResource:
    return CFNResource(
        logical_id,
        "AWS::ECS::Service",
        {
            "ServiceName": name,
            "DesiredCount": desired,
            "TaskDefinition": {"Ref": task_def_logical_id},
        },
    )


def _scalable_target(target_service_logical_id: str) -> CFNResource:
    """Build a ScalableTarget that targets the given service via the same
    Fn::Join + Fn::GetAtt shape CDK emits."""
    return CFNResource(
        "ScalableTarget",
        "AWS::ApplicationAutoScaling::ScalableTarget",
        {
            "ResourceId": {
                "Fn::Join": [
                    "",
                    [
                        "service/",
                        {"Ref": "Cluster"},
                        "/",
                        {"Fn::GetAtt": [target_service_logical_id, "Name"]},
                    ],
                ]
            },
            "ScalableDimension": "ecs:service:DesiredCount",
        },
    )


def test_autoscaled_service_logical_ids_finds_target():
    autoscaled = autoscaled_service_logical_ids(
        _result(
            _service("WorkerSvc", "WorkerTask", "treadmill-worker"),
            _service("ApiSvc", "ApiTask", "treadmill-api"),
            _scalable_target("WorkerSvc"),
        )
    )
    assert autoscaled == {"WorkerSvc"}


def test_resolve_services_skips_autoscaled_services():
    api_task = CFNResource(
        "ApiTask", "AWS::ECS::TaskDefinition",
        {
            "Family": "treadmill-api",
            "ContainerDefinitions": [{
                "Name": "api", "Image": "treadmill-api:dev",
                "PortMappings": [{"ContainerPort": 8088}],
            }],
        },
    )
    worker_task = CFNResource(
        "WorkerTask", "AWS::ECS::TaskDefinition",
        {
            "Family": "treadmill-noop-worker",
            "ContainerDefinitions": [{"Name": "noop", "Image": "treadmill-noop-worker:dev"}],
        },
    )
    services = resolve_services(
        _result(
            api_task,
            worker_task,
            _service("ApiSvc", "ApiTask", "treadmill-api"),
            _service("WorkerSvc", "WorkerTask", "treadmill-noop-worker", desired=0),
            _scalable_target("WorkerSvc"),
        ),
        {},
        _network(),
    )
    assert len(services) == 1
    assert services[0].family == "treadmill-api"
    assert services[0].desired_count == 1
    assert services[0].port_mappings == [(8088, 8088)]


def test_resolve_services_applies_default_host_port_overrides():
    """Postgres on 5432 is shifted to 15432 to avoid clashing with local
    installs; Redis on 6379 to 16379."""
    pg_task = CFNResource(
        "PgTask", "AWS::ECS::TaskDefinition",
        {
            "Family": "treadmill-postgres",
            "ContainerDefinitions": [{
                "Name": "postgres", "Image": "postgres:16-alpine",
                "PortMappings": [{"ContainerPort": 5432}],
            }],
        },
    )
    redis_task = CFNResource(
        "RdTask", "AWS::ECS::TaskDefinition",
        {
            "Family": "treadmill-redis",
            "ContainerDefinitions": [{
                "Name": "redis", "Image": "redis:7-alpine",
                "PortMappings": [{"ContainerPort": 6379}],
            }],
        },
    )
    services = resolve_services(
        _result(
            pg_task, redis_task,
            _service("PgSvc", "PgTask", "treadmill-postgres"),
            _service("RdSvc", "RdTask", "treadmill-redis"),
        ),
        {},
        _network(),
    )
    by_family = {s.family: s for s in services}
    assert by_family["treadmill-postgres"].port_mappings == [(5432, 15432)]
    assert by_family["treadmill-redis"].port_mappings == [(6379, 16379)]


def test_resolve_services_returns_empty_when_no_services():
    assert resolve_services(_result(), {}, _network()) == []
