"""Re-export of ``OutputKind`` from its canonical location.

The enum lives in :mod:`treadmill_api.models.workflow` alongside the
``Workflow`` and ``Role`` models. This module exists so callers can
import the enum via the shorter path ``treadmill_api.output_kind``
without coupling to the models package — useful for the runner-side
dispositions that route on this enum but don't otherwise depend on
the workflow model.

Per ADR-0022 the values are: ``code``, ``review``, ``analysis``,
``plan_doc``, ``documentation``. ADR-0032 added ``documentation``.
"""

from treadmill_api.models.workflow import OutputKind

__all__ = ["OutputKind"]
