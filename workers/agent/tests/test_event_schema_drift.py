"""Schema-drift contract test between worker and API.

The worker validates outbound event payloads against the API's typed
Pydantic classes before publishing (Phase 2, work item A.1). This file
locks in the import surface: if a future API refactor renames, removes,
or re-homes one of these types, this test fails before the worker is
even exercised.

See ``docs/plans/2026-05-11-week-2-closure.md`` work item A.2.
"""

from __future__ import annotations


def test_event_registry_contains_step_lifecycle_pairs() -> None:
    """The four step-lifecycle pairs the worker publishes must be
    registered. ``parse_payload`` resolves through this registry."""
    from treadmill_api.events.registry import EVENT_REGISTRY

    for pair in [
        ("step", "ready"),
        ("step", "started"),
        ("step", "completed"),
        ("step", "failed"),
    ]:
        assert pair in EVENT_REGISTRY, (
            f"missing required event registry entry {pair!r} — API refactor "
            "broke the worker contract"
        )


def test_step_output_envelope_is_importable() -> None:
    """Per ADR-0012, the uniform ``StepOutput`` envelope replaces the
    Week-2-closure ``AuthorStepOutput`` class. The worker constructs the
    envelope; the consumer reads it. Both share the API's Pydantic class
    via the workspace source dep."""
    from treadmill_api.events.step_output import (
        Artifact,
        Metadata,
        StepOutput,
    )

    assert StepOutput is not None
    assert Artifact is not None
    assert Metadata is not None


def test_author_step_output_is_no_longer_importable() -> None:
    """Per ADR-0012, ``AuthorStepOutput`` is removed; its fields demote
    into convention (``commit_sha`` top-level, ``branch`` and ``pr_url``
    as ``Artifact``s, ``pr_number`` in ``payload``). Re-introducing it as
    a separate Pydantic class would mean someone reversed the architectural
    decision — this test guards against that."""
    import pytest

    with pytest.raises(ImportError):
        from treadmill_api.events.step import AuthorStepOutput  # noqa: F401
    with pytest.raises(ImportError):
        from treadmill_api.events import AuthorStepOutput  # noqa: F401
    with pytest.raises(ImportError):
        from treadmill_agent.events import AuthorStepOutput  # noqa: F401
