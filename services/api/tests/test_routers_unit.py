"""In-process unit tests for router 500-paths.

These verify the explicit-raise replacements introduced for C.3: each
``assert <thing> is not None`` that used to mask a data-corruption
condition now raises a clean 500 with a message naming the missing
referent.

The integration-test suite (``test_integration_routers.py``,
``test_integration_steps_router.py``) covers the happy paths via real
HTTP + Postgres. These tests target the failure paths that are awkward
to provoke against a live database — we directly drive the async route
handlers with monkeypatched dependencies.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from treadmill_api.routers import roles as roles_router


class _StubSession:
    """Minimal async-session stub.

    The roles handlers we exercise here delegate the heavy lifting to
    ``_load_role_with_refs`` (which we monkeypatch) and
    ``_validate_refs_exist`` (which is a no-op for empty skill/hook
    lists). The few session methods touched along the way — ``add``,
    ``flush``, ``commit``, ``execute`` — are stubbed to record calls.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """``_validate_refs_exist`` only calls execute when there are
        skills/hooks to validate; we feed it an empty-list role so this
        path is never hit. Raise loudly if it is."""
        raise AssertionError("session.execute called unexpectedly in unit test")


@pytest.mark.asyncio
async def test_create_role_500s_cleanly_if_load_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``_load_role_with_refs`` returns None after commit (e.g. a
    concurrent delete races with our read-back), the handler must
    raise a clean 500 — not crash on tuple unpacking of None."""

    async def _none(_session: Any, _role_id: str) -> None:
        return None

    monkeypatch.setattr(roles_router, "_load_role_with_refs", _none)

    body = roles_router.RoleCreateRequest(
        id="role-vanished",
        model="claude",
        system_prompt="be a coder",
    )
    session = _StubSession()

    with pytest.raises(HTTPException) as exc_info:
        await roles_router.create_role(body=body, session=session)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 500
    assert "role-vanished" in exc_info.value.detail


@pytest.mark.asyncio
async def test_list_roles_500s_cleanly_if_load_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape for the list endpoint: a Role appears in the listing
    query but vanishes before the per-role load. The handler raises a
    clean 500 naming the missing role."""

    class _Row:
        id = "role-ghost"

    class _Scalars:
        def __iter__(self):
            return iter([_Row()])

    class _Result:
        def scalars(self):
            return _Scalars()

    class _ListingSession:
        async def execute(self, *args: Any, **kwargs: Any) -> _Result:
            return _Result()

    async def _none(_session: Any, _role_id: str) -> None:
        return None

    monkeypatch.setattr(roles_router, "_load_role_with_refs", _none)

    with pytest.raises(HTTPException) as exc_info:
        await roles_router.list_roles(session=_ListingSession())  # type: ignore[arg-type]

    assert exc_info.value.status_code == 500
    assert "role-ghost" in exc_info.value.detail
