"""ADR-0070 substep 2 — triage-finding review queue mounted at ``/api/v1/review/triage-finding``.

Sibling-module fan-out for the ADR-0061 ``triage_findings`` table.  Per
ADR-0070, every per-kind review surface is a thin file that builds a router
via :func:`treadmill_api.routers.review.base.build_review_router`; the
auto-discovery loop in :mod:`treadmill_api.routers.review` mounts whatever
this file exports as ``router``.  No edit to ``__init__.py`` or ``app.py``
was needed beyond the one substep-1.2 wiring of the aggregator.

This module mounts four endpoints under the review-package prefix:

  GET  /api/v1/review/triage-finding/next      — unlabeled queue, low-conf first
  GET  /api/v1/review/triage-finding/stats     — agreement + last-100 accuracy
  GET  /api/v1/review/triage-finding/{id}      — fetch one row (404 when missing)
  POST /api/v1/review/triage-finding/{id}/label — persist operator verdict

Legacy-shape adapter args
-------------------------

ADR-0061's ``TriageFindingRow`` predates ``ReviewQueueRowMixin``, so its
column names don't match the names the factory defaults to.  Instead of
overlaying a second class (the original substep-2 design, which SQLAlchemy
rejected because abstract classes can't be passed to ``select()``), we pass
three legacy-shape adapter arguments to the factory:

(a) ``confidence_attr="confidence"`` — the factory's confidence-ordering CASE
    expression reads ``getattr(row_cls, confidence_attr)``.  ADR-0070 native
    kinds inherit ``ReviewQueueRowMixin.llm_confidence`` and leave this at
    the default; ``TriageFindingRow`` uses ``confidence`` (ADR-0061-original).

(b) ``llm_label_attr=_triage_llm_label`` — the factory's stats math agrees
    operator vs LLM by comparing two SQL expressions.  ``TriageFindingRow``
    has no ``llm_label`` column today; the v1 stand-in is
    ``confidence != 'low'`` (treat anything but low-confidence as the LLM's
    "yes this is a real bug" recommendation).  Passing a callable lets the
    factory derive the expression from existing columns instead of looking
    up a non-existent attribute.

    TODO v2 (substep 3): substep 3 lands richer ``llm_label`` columns
    alongside new kinds; at that point replace this lambda with the typed
    ``llm_label`` column name and remove this note.

(c) ``id_attr="finding_id"`` — the factory writes ``WHERE pk == row_id`` on
    the per-id endpoints.  ``TriageFindingRow`` calls its primary key
    ``finding_id`` (ADR-0061-original); pass the attribute name so the
    factory targets the right column.

Unlabeled predicate (v1 vs v2)
------------------------------

The factory's ``/next`` query filters on ``verdict_col IS NULL``; with
``verdict_attr="label_is_real_bug"`` that means ``label_is_real_bug IS NULL``.
ADR-0061 treats null as the operator's "Skip" signal: a Skip-labeled row
has ``labeled_by`` + ``labeled_at`` stamped but ``label_is_real_bug`` stays
null.  Under the v1 predicate, **Skip-labeled rows re-appear in /next** —
the test suite pins this so any future predicate change (e.g. switching to
``labeled_at IS NULL`` in v2) fails the test loudly.

LabelFindingRequest re-use
--------------------------

We re-import :class:`treadmill_api.routers.triage.labels.LabelFindingRequest`
unchanged.  The factory's ``POST .../label`` handler calls
``body.model_dump()`` and splats every key onto the row via ``setattr``;
``LabelFindingRequest`` already declares the four kind-specific fields
(``label_severity``, ``label_category``, ``label_fix_in_dsl``,
``label_notes``) plus the framework-required ``labeled_by``, so all six
fields round-trip into the row's columns with no ``extra='allow'`` shim.

Viewer registry pointer
-----------------------

The frontend viewer registry (``services/dashboard/src/operator-review/``,
landing in substep 1.3 / FlipThroughLayout) maps each kind to its viewer
component.  This kind's pointer is ``triage-finding → TriageFindingViewer``;
keep that registry entry in lockstep with the prefix below.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case

from treadmill_api.models.triage_finding import TriageFindingRow
from treadmill_api.routers.review.base import build_review_router
from treadmill_api.routers.triage.labels import LabelFindingRequest
from treadmill_api.schemas.triage_finding import TriageFinding


def _triage_llm_label(cls: type) -> Any:
    """SQL expression standing in for a missing ``llm_label`` column.

    Reads ``confidence`` from the row class and emits ``True`` for any
    medium- or high-confidence row, ``False`` for low-confidence rows.
    This matches the v1 stand-in described in the module docstring.
    """
    return case((cls.confidence == "low", False), else_=True)


router = build_review_router(
    prefix="/triage-finding",
    row_cls=TriageFindingRow,
    verdict_attr="label_is_real_bug",
    llm_label_attr=_triage_llm_label,
    confidence_attr="confidence",
    id_attr="finding_id",
    label_input_model=LabelFindingRequest,
    output_model=TriageFinding,
)
