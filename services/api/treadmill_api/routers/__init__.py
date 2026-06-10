"""FastAPI routers for the Treadmill API.

Each router is a small, single-purpose module mounted under
``/api/v1/<resource>``. The app-factory in ``treadmill_api.app`` includes
each one explicitly.
"""

from treadmill_api.routers import (
    escalations,
    event_triggers,
    plans,
    schedules,
    system_status,
    tasks,
    webhooks,
    workflows,
)

__all__ = [
    "escalations",
    "event_triggers",
    "plans",
    "schedules",
    "system_status",
    "tasks",
    "webhooks",
    "workflows",
]
