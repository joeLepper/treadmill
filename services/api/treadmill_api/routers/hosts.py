"""``/api/v1/hosts`` and ``/api/v1/hosts/bindings`` — host registry (ADR-0095).

Durable home for the host registry and per-label host bindings on Core.
Identity is (label, host): the label addresses the session; the host says
where it runs. Bindings are explicit-only — there is no automatic rebind.

Endpoints:
  POST /hosts                          register or refresh a host
  GET  /hosts                          list all known hosts
  POST /hosts/bindings                 bind a label to a host
  GET  /hosts/bindings/{label}         get bound host for a label (404 if unbound)
  GET  /hosts/{host}/labels            list all labels bound to a host
  DELETE /hosts/bindings/{label}       remove a label binding
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.host_registry_store import HostRegistryStore

router = APIRouter(prefix="/api/v1", tags=["hosts"])

_store = HostRegistryStore()


# ── Wire models ───────────────────────────────────────────────────────────────


class HostRow(BaseModel):
    id: uuid.UUID
    name: str
    tailnet_addr: str | None
    created_at: datetime


class HostRegister(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    tailnet_addr: str | None = Field(default=None, max_length=64)


class BindingRow(BaseModel):
    label: str
    host: str
    bound_at: datetime
    bound_by: str | None


class BindRequest(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=255)
    bound_by: str | None = Field(default=None, max_length=255)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/hosts", response_model=HostRow, status_code=status.HTTP_200_OK)
async def register_host(
    body: HostRegister,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HostRow:
    row = await _store.register_host(session, body.name, tailnet_addr=body.tailnet_addr)
    await session.commit()
    return HostRow.model_validate(row, from_attributes=True)


@router.get("/hosts", response_model=list[HostRow])
async def list_hosts(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[HostRow]:
    rows = await _store.list_hosts(session)
    return [HostRow.model_validate(r, from_attributes=True) for r in rows]


@router.post(
    "/hosts/bindings", response_model=BindingRow, status_code=status.HTTP_200_OK
)
async def bind_label(
    body: BindRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BindingRow:
    row = await _store.bind_label(
        session, body.label, body.host, bound_by=body.bound_by
    )
    await session.commit()
    return BindingRow.model_validate(row, from_attributes=True)


@router.get("/hosts/bindings/{label}", response_model=BindingRow)
async def get_binding(
    label: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BindingRow:
    host = await _store.host_for_label(session, label)
    if host is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no binding found for label {label!r}",
        )
    # Re-fetch the full row for the wire model.
    import sqlalchemy as sa
    from treadmill_api.models.host import SessionHostBinding
    row = await session.scalar(
        sa.select(SessionHostBinding).where(SessionHostBinding.label == label)
    )
    assert row is not None
    return BindingRow.model_validate(row, from_attributes=True)


@router.get("/hosts/{host}/labels", response_model=list[str])
async def labels_on_host(
    host: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[str]:
    return await _store.labels_on_host(session, host)


@router.delete(
    "/hosts/bindings/{label}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def unbind_label(
    label: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    deleted = await _store.unbind_label(session, label)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no binding found for label {label!r}",
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
