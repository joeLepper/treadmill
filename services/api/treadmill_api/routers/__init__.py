"""FastAPI routers for the Treadmill API.

Each router is a small, single-purpose module mounted under
``/api/v1/<resource>``. The app-factory in ``treadmill_api.app`` includes
each one explicitly.
"""

from treadmill_api.routers import (
    event_triggers,
    hooks,
    plans,
    roles,
    skills,
    tasks,
    webhooks,
    workflows,
)

__all__ = [
    "event_triggers",
    "hooks",
    "plans",
    "roles",
    "skills",
    "tasks",
    "webhooks",
    "workflows",
]
