"""FastAPI routers for the Treadmill API.

Each router is a small, single-purpose module mounted under
``/api/v1/<resource>``. The app-factory in ``treadmill_api.app`` includes
each one explicitly.
"""

from treadmill_api.routers import (
    escalations,
    plans,
    schedules,
    system_status,
    tasks,
    webhooks,
)

__all__ = [
    "escalations",
    "plans",
    "schedules",
    "system_status",
    "tasks",
    "webhooks",
]
