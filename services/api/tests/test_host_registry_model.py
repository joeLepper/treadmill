"""Shape tests for the ADR-0095 host registry models, store, and router.

A non-DB structural check runs always (pure import + attribute check).
The integration round-trips against real Postgres are gated on
``TREADMILL_INTEGRATION=1`` (matching the pattern in test_onboarding_store.py).
"""

from __future__ import annotations

import os

import pytest

from treadmill_api.models.host import Host, SessionHostBinding
from treadmill_api.host_registry_store import HostRegistryStore
from treadmill_api.routers.hosts import BindRequest, HostRegister, HostRow, BindingRow


# ── Non-DB structural tests (always run) ─────────────────────────────────────


def test_host_model_tablename() -> None:
    assert Host.__tablename__ == "hosts"


def test_session_host_binding_tablename() -> None:
    assert SessionHostBinding.__tablename__ == "session_host_bindings"


def test_host_has_tailnet_addr_column() -> None:
    assert hasattr(Host, "tailnet_addr"), "Host model missing tailnet_addr column"


def test_host_tailnet_addr_is_nullable() -> None:
    col = Host.__table__.c["tailnet_addr"]
    assert col.nullable, "tailnet_addr must be nullable"


def test_store_exposes_required_methods() -> None:
    for method in (
        "register_host",
        "list_hosts",
        "bind_label",
        "host_for_label",
        "labels_on_host",
        "unbind_label",
    ):
        assert hasattr(HostRegistryStore, method), (
            f"HostRegistryStore missing {method!r}"
        )


def test_bind_request_validates_sample_payload() -> None:
    payload = BindRequest(
        label="worker-joelepper-treadmill-1",
        host="rainbow",
        bound_by="coordinator-joelepper-treadmill",
    )
    assert payload.label == "worker-joelepper-treadmill-1"
    assert payload.host == "rainbow"
    assert payload.bound_by == "coordinator-joelepper-treadmill"


def test_bind_request_bound_by_is_optional() -> None:
    payload = BindRequest(label="worker-x", host="laptop")
    assert payload.bound_by is None


def test_host_register_tailnet_addr_is_optional() -> None:
    payload = HostRegister(name="rainbow")
    assert payload.tailnet_addr is None


def test_host_register_with_tailnet_addr() -> None:
    payload = HostRegister(name="rainbow", tailnet_addr="100.64.0.1")
    assert payload.tailnet_addr == "100.64.0.1"


def test_host_row_includes_tailnet_addr_field() -> None:
    fields = HostRow.model_fields
    assert "tailnet_addr" in fields, "HostRow wire model missing tailnet_addr"


def test_binding_row_includes_bound_by_field() -> None:
    fields = BindingRow.model_fields
    assert "bound_by" in fields, "BindingRow wire model missing bound_by"


# ── Integration round-trips ──────────────────────────────────────────────────


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
integration = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason=(
        "set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL to run; "
        "requires `treadmill-local up`"
    ),
)


@integration
@pytest.mark.asyncio
async def test_register_and_list_hosts() -> None:
    import subprocess
    from pathlib import Path
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async_url = TEST_DB_URL.replace("+psycopg", "+asyncpg")  # type: ignore[union-attr]
    services_api_dir = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env={**os.environ, "DATABASE_URL": TEST_DB_URL},
        check=True,
    )
    engine = create_async_engine(async_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = HostRegistryStore()

    async with factory() as session:
        host = await store.register_host(session, "rainbow", tailnet_addr="100.64.0.1")
        await session.commit()
    assert host.name == "rainbow"
    assert host.tailnet_addr == "100.64.0.1"

    async with factory() as session:
        hosts = await store.list_hosts(session)
    names = [h.name for h in hosts]
    assert "rainbow" in names

    await engine.dispose()


@integration
@pytest.mark.asyncio
async def test_bind_label_and_query() -> None:
    import subprocess
    from pathlib import Path
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async_url = TEST_DB_URL.replace("+psycopg", "+asyncpg")  # type: ignore[union-attr]
    services_api_dir = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env={**os.environ, "DATABASE_URL": TEST_DB_URL},
        check=True,
    )
    engine = create_async_engine(async_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = HostRegistryStore()

    async with factory() as session:
        await store.bind_label(
            session, "worker-test-1", "rainbow",
            bound_by="coordinator-test",
        )
        await session.commit()

    async with factory() as session:
        host = await store.host_for_label(session, "worker-test-1")
    assert host == "rainbow"

    async with factory() as session:
        labels = await store.labels_on_host(session, "rainbow")
    assert "worker-test-1" in labels

    async with factory() as session:
        deleted = await store.unbind_label(session, "worker-test-1")
        await session.commit()
    assert deleted is True

    async with factory() as session:
        host_after = await store.host_for_label(session, "worker-test-1")
    assert host_after is None

    await engine.dispose()
