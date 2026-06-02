"""Unit tests for the Autoscaler tick logic and ScalableTarget bounds parser."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from treadmill_local.autoscaler import (
    Autoscaler,
    _container_age_seconds,
    _REAP_AGE_SECONDS,
    parse_scalable_target_bounds,
)


class _FakeDockerAdapter:
    """Test double for DockerClientAdapter — no real Docker daemon required.

    Records all calls so tests can assert on network and container operations
    without touching the Docker socket.
    """

    def __init__(self) -> None:
        self.networks_ensured: list[tuple[str, bool]] = []
        self.containers_run: list[dict[str, Any]] = []
        self.running_containers: set[str] = set()
        # name -> ip; populated by tests to simulate assigned IPs
        self.container_ips: dict[str, str] = {}
        # (network_name, container_name) tuples recorded by
        # ``connect_container_to_network`` (ADR-0064 Step 2).
        self.network_attachments: list[tuple[str, str]] = []

    def ensure_network(self, name: str, *, internal: bool = False) -> None:
        self.networks_ensured.append((name, internal))

    def container_running(self, name: str) -> bool:
        return name in self.running_containers

    def run_container(self, image: str, *, name: str, **kwargs: Any) -> MagicMock:
        self.containers_run.append({"image": image, "name": name, **kwargs})
        c = MagicMock()
        c.name = name
        return c

    def get_container_ip(self, container: Any, network_name: str) -> str | None:
        return self.container_ips.get(container.name)

    def connect_container_to_network(self, name: str, container: Any) -> None:
        self.network_attachments.append((name, container.name))


class _Fake:
    """Test double exposing the four callables Autoscaler depends on, plus
    mutable workload counts and counters of starts and reaps.

    ``queue_depth`` returns ``(visible, in_flight)`` to match the shape
    the real SQS closure returns. The Autoscaler sums them.
    """

    def __init__(
        self,
        visible: int = 0,
        in_flight: int = 0,
        current: int = 0,
        reap_return: int = 0,
    ):
        self.visible = visible
        self.in_flight = in_flight
        self.current = current
        self.starts = 0
        self.reap_calls = 0
        self.reap_return = reap_return

    def queue_depth(self) -> tuple[int, int]:
        return self.visible, self.in_flight

    def worker_count(self) -> int:
        return self.current

    def start_worker(self) -> None:
        self.starts += 1
        # Simulate the worker becoming visible to docker ps.
        self.current += 1

    def reap_dead_workers(self) -> int:
        self.reap_calls += 1
        return self.reap_return


def _autoscaler(
    fake: _Fake,
    *,
    min_count: int = 0,
    max_count: int = 1,
    with_reap: bool = False,
) -> Autoscaler:
    return Autoscaler(
        queue_depth_fn=fake.queue_depth,
        worker_count_fn=fake.worker_count,
        start_worker_fn=fake.start_worker,
        reap_dead_workers_fn=fake.reap_dead_workers if with_reap else None,
        min_count=min_count,
        max_count=max_count,
        tick_seconds=0.0,
    )


# ── tick logic ────────────────────────────────────────────────────────────────


def test_tick_starts_worker_when_visible_exceeds_current():
    fake = _Fake(visible=1, current=0)
    a = _autoscaler(fake)
    snap = a.tick()
    assert snap.visible == 1
    assert snap.in_flight == 0
    assert snap.total == 1
    assert snap.desired == 1
    assert snap.started == 1
    assert fake.starts == 1


def test_tick_caps_starts_at_max():
    fake = _Fake(visible=10, current=0)
    a = _autoscaler(fake, max_count=3)
    snap = a.tick()
    assert snap.desired == 3
    assert snap.started == 3
    assert fake.starts == 3


def test_tick_does_not_start_when_desired_le_current():
    fake = _Fake(visible=5, current=2)
    a = _autoscaler(fake, max_count=2)
    snap = a.tick()
    assert snap.desired == 2
    assert snap.started == 0
    assert fake.starts == 0


def test_tick_zero_workload_zero_workers():
    fake = _Fake(visible=0, in_flight=0, current=0)
    a = _autoscaler(fake)
    snap = a.tick()
    assert snap.desired == 0
    assert snap.started == 0


def test_tick_respects_min_count():
    """If min=2 and workload=0, desired floors at 2 — so the loop will start
    workers up to the minimum even with no work."""
    fake = _Fake(visible=0, in_flight=0, current=0)
    a = _autoscaler(fake, min_count=2, max_count=5)
    snap = a.tick()
    assert snap.desired == 2
    assert snap.started == 2


def test_tick_natural_drain_when_workload_drops():
    """Workers exit after each step. When workload falls, the loop simply
    does not start replacements; current decays naturally as workers finish."""
    fake = _Fake(visible=3, current=1)  # one worker already running
    a = _autoscaler(fake, max_count=1)
    snap = a.tick()
    assert snap.desired == 1
    assert snap.started == 0  # already at max

    # Worker finishes and exits — outside the autoscaler's control.
    fake.current = 0
    fake.visible = 0
    snap = a.tick()
    assert snap.desired == 0
    assert snap.started == 0


# ── total-workload sizing (the bug this PR fixes) ─────────────────────────────


def test_tick_sums_visible_and_in_flight_for_desired():
    """visible=3, in_flight=2 → total=5; with max=8 we want desired=5.

    This is the property that distinguishes the new behavior from the
    visible-only sizing it replaces.
    """
    fake = _Fake(visible=3, in_flight=2, current=0)
    a = _autoscaler(fake, max_count=8)
    snap = a.tick()
    assert snap.total == 5
    assert snap.desired == 5


def test_tick_sizes_on_in_flight_alone_when_visible_is_zero():
    """The motivating case: a long-running message holds a worker, the
    queue is otherwise empty, but the autoscaler should still keep the
    in-flight worker counted so it doesn't scale down prematurely. With
    visible=0, in_flight=5, max=8 we expect desired=5."""
    fake = _Fake(visible=0, in_flight=5, current=0)
    a = _autoscaler(fake, max_count=8)
    snap = a.tick()
    assert snap.visible == 0
    assert snap.in_flight == 5
    assert snap.total == 5
    assert snap.desired == 5


def test_tick_caps_total_at_max():
    """visible=10 + in_flight=2 = 12, capped to max=8."""
    fake = _Fake(visible=10, in_flight=2, current=0)
    a = _autoscaler(fake, max_count=8)
    snap = a.tick()
    assert snap.total == 12
    assert snap.desired == 8


def test_tick_zero_workload_means_zero_desired():
    """visible=0 + in_flight=0 → desired=min_count=0."""
    fake = _Fake(visible=0, in_flight=0, current=0)
    a = _autoscaler(fake, min_count=0, max_count=8)
    snap = a.tick()
    assert snap.total == 0
    assert snap.desired == 0


# ── invariants on construction ────────────────────────────────────────────────


def test_constructor_rejects_negative_min():
    with pytest.raises(ValueError, match="min_count must be >= 0"):
        _autoscaler(_Fake(), min_count=-1)


def test_constructor_rejects_max_below_min():
    with pytest.raises(ValueError, match="must be >= min_count"):
        _autoscaler(_Fake(), min_count=5, max_count=3)


# ── ScalableTarget bounds parsing ─────────────────────────────────────────────


def test_parse_bounds_present():
    assert parse_scalable_target_bounds({"MinCapacity": 0, "MaxCapacity": 4}) == (0, 4)


def test_parse_bounds_missing_use_defaults():
    assert parse_scalable_target_bounds({}) == (0, 1)


def test_parse_bounds_partial():
    assert parse_scalable_target_bounds({"MaxCapacity": 7}) == (0, 7)
    assert parse_scalable_target_bounds({"MinCapacity": 2}) == (2, 1)
    # ^ Note: that's (2, 1), the parser does not enforce min<=max — Autoscaler
    # constructor catches that and raises.


def test_parse_bounds_rejects_non_int():
    with pytest.raises(TypeError):
        parse_scalable_target_bounds({"MinCapacity": "0", "MaxCapacity": 1})


# ── reap-on-tick integration with Autoscaler ──────────────────────────────────


def test_tick_reports_zero_reaped_by_default():
    """No reap_dead_workers_fn provided → snap.reaped is 0, the default."""
    fake = _Fake(visible=0, current=0)
    a = _autoscaler(fake)  # no reap closure
    snap = a.tick()
    assert snap.reaped == 0


def test_tick_calls_reap_when_provided_and_reports_count():
    fake = _Fake(visible=0, current=0, reap_return=0)
    a = _autoscaler(fake, with_reap=True)
    snap = a.tick()
    assert fake.reap_calls == 1
    assert snap.reaped == 0


def test_tick_reports_reaped_count_when_nonzero():
    fake = _Fake(visible=0, current=0, reap_return=4)
    a = _autoscaler(fake, with_reap=True)
    snap = a.tick()
    assert snap.reaped == 4


def test_tick_reaping_does_not_perturb_scaling_decision():
    """Scaling decision is independent of reap output. visible=2, current=0,
    max=2 → start 2 workers and also report whatever reap returns."""
    fake = _Fake(visible=2, current=0, reap_return=7)
    a = _autoscaler(fake, max_count=2, with_reap=True)
    snap = a.tick()
    assert snap.desired == 2
    assert snap.started == 2
    assert fake.starts == 2
    assert snap.reaped == 7


def _run_one_tick(a: Autoscaler) -> None:
    """Run exactly one iteration of ``a.run()`` then stop.

    The trick: queue_depth_fn is called first inside tick(); wrap it
    so it triggers a.stop() after returning. The loop then runs one
    iteration, logs the tick line, and exits on the next predicate
    check.
    """
    original = a.queue_depth_fn

    def stop_after() -> int:
        value = original()
        a.stop()
        return value

    a.queue_depth_fn = stop_after
    a.run()


def test_run_log_includes_reaped_column(caplog):
    """The per-tick log line carries the new reaped=N column."""
    fake = _Fake(visible=0, current=0, reap_return=2)
    a = _autoscaler(fake, with_reap=True)
    with caplog.at_level(logging.INFO, logger="treadmill.autoscaler"):
        _run_one_tick(a)
    tick_lines = [r.getMessage() for r in caplog.records if "tick:" in r.getMessage()]
    assert tick_lines, "expected at least one tick log line"
    assert "reaped=2" in tick_lines[-1]


def test_run_log_reaped_zero_renders_in_tick_line(caplog):
    fake = _Fake(visible=0, current=0, reap_return=0)
    a = _autoscaler(fake, with_reap=True)
    with caplog.at_level(logging.INFO, logger="treadmill.autoscaler"):
        _run_one_tick(a)
    tick_lines = [r.getMessage() for r in caplog.records if "tick:" in r.getMessage()]
    assert tick_lines
    assert "reaped=0" in tick_lines[-1]


def test_run_log_includes_visible_in_flight_total_breakdown(caplog):
    """The per-tick log line carries the new visible/in_flight/total breakdown
    so operators can see at a glance whether the autoscaler is responding
    to real workload (in-flight) or unclaimed work (visible)."""
    fake = _Fake(visible=2, in_flight=3, current=0, reap_return=0)
    a = _autoscaler(fake, max_count=8, with_reap=True)
    with caplog.at_level(logging.INFO, logger="treadmill.autoscaler"):
        _run_one_tick(a)
    tick_lines = [r.getMessage() for r in caplog.records if "tick:" in r.getMessage()]
    assert tick_lines
    line = tick_lines[-1]
    assert "visible=2" in line
    assert "in_flight=3" in line
    assert "total=5" in line
    assert "desired=5" in line


# ── _container_age_seconds parser ─────────────────────────────────────────────


def test_container_age_seconds_handles_iso_with_z():
    finished = "2026-05-19T12:00:00.000000Z"
    now = datetime(2026, 5, 19, 12, 0, 45, tzinfo=timezone.utc).timestamp()
    age = _container_age_seconds(finished, now)
    assert age is not None
    assert 44.5 <= age <= 45.5


def test_container_age_seconds_handles_nanoseconds():
    """Docker emits 9-digit fractional seconds; Python only supports 6."""
    finished = "2026-05-19T12:00:00.123456789Z"
    now = datetime(2026, 5, 19, 12, 0, 30, tzinfo=timezone.utc).timestamp()
    age = _container_age_seconds(finished, now)
    assert age is not None
    assert 29 <= age <= 30


def test_container_age_seconds_returns_none_for_sentinel():
    assert _container_age_seconds("0001-01-01T00:00:00Z", 0.0) is None


def test_container_age_seconds_returns_none_for_missing():
    assert _container_age_seconds(None, 0.0) is None
    assert _container_age_seconds("", 0.0) is None


def test_container_age_seconds_returns_none_for_garbage():
    assert _container_age_seconds("not-a-timestamp", 0.0) is None


# ── reap closure against a fake docker client ─────────────────────────────────


def _iso(dt: datetime) -> str:
    """Render a datetime in the Z-suffixed form Docker uses."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fake_container(name: str, finished_at: str) -> MagicMock:
    c = MagicMock()
    c.name = name
    c.attrs = {"State": {"FinishedAt": finished_at}}
    return c


def test_reap_closure_removes_only_old_exited_containers(monkeypatch):
    """Build the reap closure the way main() does, point it at a fake
    docker client, and assert .remove() fires only on containers whose
    FinishedAt is older than _REAP_AGE_SECONDS."""
    import docker as _docker_pkg  # for the APIError class

    from treadmill_local import autoscaler as autoscaler_mod

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    # Two old (60s, 120s) and one fresh (5s ago, below the 30s threshold).
    old_a = _fake_container("worker-old-a", _iso(now - timedelta(seconds=60)))
    old_b = _fake_container("worker-old-b", _iso(now - timedelta(seconds=120)))
    fresh = _fake_container("worker-fresh", _iso(now - timedelta(seconds=5)))

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [old_a, old_b, fresh]

    # Freeze time.time() so the closure compares against `now`.
    monkeypatch.setattr(autoscaler_mod.time, "time", lambda: now.timestamp())

    family = "demo"
    worker_labels = [
        "treadmill.managed=true",
        "treadmill.role=worker",
        f"treadmill.family={family}",
    ]

    def reap_dead_workers() -> int:
        ts = autoscaler_mod.time.time()
        exited = fake_client.containers.list(
            filters={"label": worker_labels, "status": "exited"}
        )
        reaped = 0
        for container in exited:
            finished_at = container.attrs.get("State", {}).get("FinishedAt")
            age = autoscaler_mod._container_age_seconds(finished_at, ts)
            if age is None or age < _REAP_AGE_SECONDS:
                continue
            try:
                container.remove()
                reaped += 1
            except _docker_pkg.errors.APIError:
                pass
        return reaped

    n = reap_dead_workers()
    assert n == 2
    old_a.remove.assert_called_once_with()
    old_b.remove.assert_called_once_with()
    fresh.remove.assert_not_called()

    # Confirm the label filter the closure sends to docker — this is the
    # invariant that prevents mass-pruning unrelated containers.
    fake_client.containers.list.assert_called_once_with(
        filters={
            "label": [
                "treadmill.managed=true",
                "treadmill.role=worker",
                f"treadmill.family={family}",
            ],
            "status": "exited",
        }
    )


def test_reap_closure_swallows_remove_failures(monkeypatch):
    """A single failed remove() must not break the loop or undercount the
    successful reaps."""
    import docker as _docker_pkg

    from treadmill_local import autoscaler as autoscaler_mod

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    good = _fake_container("worker-good", _iso(now - timedelta(seconds=60)))
    bad = _fake_container("worker-bad", _iso(now - timedelta(seconds=60)))
    bad.remove.side_effect = _docker_pkg.errors.APIError("boom")

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [good, bad]
    monkeypatch.setattr(autoscaler_mod.time, "time", lambda: now.timestamp())

    def reap_dead_workers() -> int:
        ts = autoscaler_mod.time.time()
        exited = fake_client.containers.list(
            filters={"label": [], "status": "exited"}
        )
        reaped = 0
        for container in exited:
            age = autoscaler_mod._container_age_seconds(
                container.attrs.get("State", {}).get("FinishedAt"), ts
            )
            if age is None or age < _REAP_AGE_SECONDS:
                continue
            try:
                container.remove()
                reaped += 1
            except _docker_pkg.errors.APIError:
                pass
        return reaped

    assert reap_dead_workers() == 1
    good.remove.assert_called_once_with()
    bad.remove.assert_called_once_with()


# ── workload closure against a fake SQS client ────────────────────────────────


def test_get_depth_closure_returns_visible_and_in_flight_in_one_call():
    """The closure built in main() asks SQS for both attribute counts in a
    single get_queue_attributes call and returns them as a tuple. Verifying
    this here pins the contract the Autoscaler depends on — visible-only
    sizing under-provisioned, see ADR-0018."""

    queue_url = "https://sqs.example/queue.fifo"
    fake_sqs = MagicMock()
    fake_sqs.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "4",
            "ApproximateNumberOfMessagesNotVisible": "7",
        }
    }

    def get_depth() -> tuple[int, int]:
        attrs = fake_sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        visible = int(attrs.get("ApproximateNumberOfMessages", "0"))
        in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0"))
        return visible, in_flight

    visible, in_flight = get_depth()
    assert visible == 4
    assert in_flight == 7

    # One API call per tick — both attributes requested together.
    fake_sqs.get_queue_attributes.assert_called_once_with(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )


def test_get_depth_closure_defaults_missing_attributes_to_zero():
    """If SQS omits an attribute (shouldn't happen for these two, but be
    defensive), the closure treats it as zero rather than raising."""
    fake_sqs = MagicMock()
    fake_sqs.get_queue_attributes.return_value = {"Attributes": {}}

    def get_depth() -> tuple[int, int]:
        attrs = fake_sqs.get_queue_attributes(
            QueueUrl="q",
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        visible = int(attrs.get("ApproximateNumberOfMessages", "0"))
        in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0"))
        return visible, in_flight

    assert get_depth() == (0, 0)


# ── egress proxy helpers (ADR-0060) ───────────────────────────────────────────


def test_ensure_egress_network_creates_internal_network() -> None:
    from treadmill_local.egress_proxy import (
        EGRESS_NETWORK_NAME,
        ensure_egress_network,
    )

    adapter = _FakeDockerAdapter()
    ensure_egress_network(adapter)
    assert adapter.networks_ensured == [(EGRESS_NETWORK_NAME, True)]


def test_ensure_egress_network_is_idempotent_when_network_exists() -> None:
    """ADR-0064 Step 1: the boot path calls ``ensure_egress_network``
    before services start; the autoscaler also calls it on its own
    startup. The helper must be safe to call when the network already
    exists — the underlying ``DockerClientAdapter.ensure_network``
    catches NotFound on ``get`` and only creates on miss, so repeat
    calls return the existing network without raising."""
    from treadmill_local.docker_client import DockerClientAdapter
    from treadmill_local.egress_proxy import (
        EGRESS_NETWORK_NAME,
        ensure_egress_network,
    )

    existing_network = MagicMock(name="existing_network")
    fake_client = MagicMock(name="fake_docker_client")
    fake_client.networks.get.return_value = existing_network

    adapter = DockerClientAdapter(client=fake_client)
    ensure_egress_network(adapter)
    ensure_egress_network(adapter)

    # The adapter looks the network up; both calls hit the get path
    # and neither attempts to create (the existing-network branch).
    assert fake_client.networks.get.call_count == 2
    fake_client.networks.get.assert_any_call(EGRESS_NETWORK_NAME)
    fake_client.networks.create.assert_not_called()


def test_ensure_egress_proxy_container_spawns_when_not_running(
    tmp_path: Path,
) -> None:
    from treadmill_local.egress_proxy import (
        EGRESS_NETWORK_NAME,
        EGRESS_PROXY_CONTAINER_NAME,
        EGRESS_PROXY_IMAGE,
        ensure_egress_proxy_container,
    )
    from treadmill_local.runtime import NETWORK_NAME

    adapter = _FakeDockerAdapter()
    ensure_egress_proxy_container(adapter, tmp_path)
    assert len(adapter.containers_run) == 1
    run = adapter.containers_run[0]
    assert run["image"] == EGRESS_PROXY_IMAGE
    assert run["name"] == EGRESS_PROXY_CONTAINER_NAME
    assert run["network"] == EGRESS_NETWORK_NAME

    # ADR-0064 Step 2: the proxy is multi-attached to ``treadmill-local``
    # after spawn so its outbound CONNECTs route through that network's
    # gateway — the ``treadmill-egress`` bridge is ``internal=True`` and
    # has no upstream egress path. Pinning the attach here prevents a
    # silent revert that would re-break worker traffic.
    assert (NETWORK_NAME, EGRESS_PROXY_CONTAINER_NAME) in adapter.network_attachments


def test_ensure_egress_proxy_container_skips_when_already_running(
    tmp_path: Path,
) -> None:
    from treadmill_local.egress_proxy import (
        EGRESS_PROXY_CONTAINER_NAME,
        ensure_egress_proxy_container,
    )

    adapter = _FakeDockerAdapter()
    adapter.running_containers.add(EGRESS_PROXY_CONTAINER_NAME)
    ensure_egress_proxy_container(adapter, tmp_path)
    assert adapter.containers_run == []


def test_mint_worker_credential_returns_token_and_sha256() -> None:
    import hashlib

    from treadmill_local.egress_proxy import mint_worker_credential

    token, token_hash = mint_worker_credential()
    assert len(token) > 0
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert len(token_hash) == 64


def test_worker_proxy_env_bypasses_internal_hosts() -> None:
    """ADR-0060 follow-up: workers route internal calls (treadmill-api,
    localhost) DIRECT — not through the CONNECT-only egress proxy that
    returns 400 on plain-HTTP traffic. Regression check for the
    2026-06-02 worker crashloop on installation-token mint."""
    from treadmill_local.egress_proxy import worker_proxy_env

    env = worker_proxy_env("test-credential-xyz")

    # Proxy is set for external traffic.
    assert env["HTTP_PROXY"] == "http://treadmill-egress-proxy:3128"
    assert env["HTTPS_PROXY"] == "http://treadmill-egress-proxy:3128"

    # Internal hosts bypass the proxy. Both casings present — Python's
    # urllib honors lowercase no_proxy in some code paths.
    assert "treadmill-api" in env["NO_PROXY"]
    assert "treadmill-api" in env["no_proxy"]
    assert "localhost" in env["NO_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]

    # Install credential is threaded through unchanged.
    assert env["TREADMILL_INSTALL_PROXY_TOKEN"] == "test-credential-xyz"


def test_build_always_allowed_includes_static_and_api_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from treadmill_local.egress_proxy import _ALWAYS_ALLOWED_STATIC, build_always_allowed

    monkeypatch.setenv("TREADMILL_API_HOST", "api.example.treadmill.dev")
    result = build_always_allowed()
    for h in _ALWAYS_ALLOWED_STATIC:
        assert h in result
    assert "api.example.treadmill.dev" in result


def test_build_always_allowed_omits_api_host_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from treadmill_local.egress_proxy import _ALWAYS_ALLOWED_STATIC, build_always_allowed

    monkeypatch.delenv("TREADMILL_API_HOST", raising=False)
    result = build_always_allowed()
    assert result == list(_ALWAYS_ALLOWED_STATIC)


def test_build_install_allowed_merges_defaults_and_urls() -> None:
    from treadmill_local.egress_proxy import INSTALL_DEFAULTS, build_install_allowed

    urls = ["https://example.com/tool.tar.gz", "https://cdn.example.org/bin"]
    result = build_install_allowed(urls)
    for h in INSTALL_DEFAULTS:
        assert h in result
    assert "example.com" in result
    assert "cdn.example.org" in result


def test_write_worker_allowlist_writes_valid_json(tmp_path: Path) -> None:
    from treadmill_local.egress_proxy import write_worker_allowlist

    config_dir = tmp_path / "egress-proxy-config"
    write_worker_allowlist(
        config_dir,
        worker_ip="10.0.1.5",
        credential_hash="abc123",
        always_allowed=["api.anthropic.com"],
        install_allowed=["pypi.org"],
    )
    out = json.loads((config_dir / "10.0.1.5.json").read_text())
    assert out["worker_ip"] == "10.0.1.5"
    assert out["install_credential_hash"] == "abc123"
    assert "api.anthropic.com" in out["always_allowed"]
    assert "pypi.org" in out["install_allowed"]
