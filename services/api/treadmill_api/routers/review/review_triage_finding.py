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

Three legacy-shape adapters
---------------------------

ADR-0061's ``TriageFindingRow`` predates ``ReviewQueueRowMixin``, so its
column names don't match the names the factory hardcodes.  We bridge the gap
without touching the model file:

(a) ``confidence_attr="confidence"`` — the factory's confidence-ordering CASE
    expression reads ``getattr(row_cls, confidence_attr)``.  ADR-0070-native
    kinds inherit ``ReviewQueueRowMixin.llm_confidence`` and leave this at
    the default; ``TriageFindingRow`` uses ``confidence`` (ADR-0061-original)
    so we pass the override.  New kinds should OMIT this argument.

(b) ``llm_label`` — the factory's stats math reads
    ``getattr(row_cls, llm_label_attr)`` to compute operator/LLM agreement.
    ``TriageFindingRow`` has no ``llm_label`` column today; the v1 stand-in
    is ``confidence != 'low'`` (treat anything but low-confidence as the
    LLM's "yes this is a real bug" recommendation).  Implemented as a
    :class:`sqlalchemy.ext.hybrid.hybrid_property` on the overlay class
    so the expression form composes inside ``select`` / ``where`` clauses
    while the instance form works on loaded rows.

    TODO v2 (substep 3): substep 3 lands richer ``llm_label`` columns
    alongside new kinds; at that point replace the ``confidence`` alias with
    the typed ``llm_label`` column and remove this note.

(c) ``id`` — the factory's ``select(row_cls).where(row_cls.id == row_id)``
    references an ``id`` attribute, but ``TriageFindingRow`` calls its
    primary key ``finding_id`` (ADR-0061-original).  The overlay exposes
    ``id`` as a hybrid_property aliasing ``finding_id`` so the factory
    compiles its SQL against the correct column.

Overlay class
-------------

:class:`_TriageFindingReviewRow` subclasses :class:`TriageFindingRow` with
``__abstract__ = True`` — no new mapper, no second mapped table; SQLAlchemy
inherits the parent's mapper via MRO so ``select(_TriageFindingReviewRow)``
returns ``TriageFindingRow`` instances over the same ``triage_findings``
table.  All queries hit ``triage_findings`` directly; the overlay only
provides the three attribute shims described above.

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

from sqlalchemy.ext.hybrid import hybrid_property

from treadmill_api.models.triage_finding import TriageFindingRow
from treadmill_api.routers.review.base import build_review_router
from treadmill_api.routers.triage.labels import LabelFindingRequest
from treadmill_api.schemas.triage_finding import TriageFinding


class _TriageFindingReviewRow(TriageFindingRow):
    """Thin overlay adding ``llm_label`` + ``id`` aliases to ``TriageFindingRow``.

    ``__abstract__ = True`` keeps SQLAlchemy from creating a second mapped
    table — the overlay inherits the parent's mapper via MRO, so all
    queries hit the same ``triage_findings`` table and return
    ``TriageFindingRow`` instances.
    """

    __abstract__ = True

    @hybrid_property
    def llm_label(self) -> bool:
        """Instance form: treat any non-low confidence as the LLM's
        'yes this is a real bug' recommendation.
        """
        return self.confidence != "low"

    @llm_label.expression  # type: ignore[no-redef]
    @classmethod
    def llm_label(cls) -> Any:  # noqa: F811
        """Class form: the SQL expression compute_stats compares against
        ``label_is_real_bug`` to count operator/LLM agreement.
        """
        return cls.confidence != "low"

    @hybrid_property
    def id(self) -> Any:
        """Instance form: alias for ``finding_id`` (ADR-0061's PK name)."""
        return self.finding_id

    @id.expression  # type: ignore[no-redef]
    @classmethod
    def id(cls) -> Any:  # noqa: F811
        """Class form: lets the factory write ``select(row_cls).where(row_cls.id == ...)``
        even though the underlying column is named ``finding_id``.

        The ``.label("id")`` is required so that subqueries built via
        ``select(row_cls.id).subquery()`` expose a column named ``id``
        (not ``finding_id``), matching the ``last_100_subq.c.id`` access
        in ``review_stats.compute_stats``.
        """
        return cls.finding_id.label("id")


router = build_review_router(
    prefix="/triage-finding",
    row_cls=_TriageFindingReviewRow,
    verdict_attr="label_is_real_bug",
    llm_label_attr="llm_label",
    confidence_attr="confidence",
    label_input_model=LabelFindingRequest,
    output_model=TriageFinding,
)
